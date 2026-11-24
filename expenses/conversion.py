"""Converting expenses into the currency the tracker is shown in, with one exchange rate per month.

Precision rules:
- Rates are Decimals with 8 decimal places (PKR -> USD is ~0.0036, so 4 places would lose accuracy).
- Every expense is converted and rounded to the cent on its own; totals are sums of those rounded amounts,
  so the list always adds up to the total exactly.
- A month's market rate is stored the first time it is needed and reused, so past months never shift.
"""
import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.rates import market_rate, user_rate_settings

from .models import MonthlyRate

CENT = Decimal("0.01")
RATE_PLACES = Decimal("0.00000001")


def to_cents(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def month_end(first):
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


class Converter:
    """Looks up (and stores) the monthly rate for each currency pair; converts amounts to `display`."""

    def __init__(self, user, display):
        self.user = user
        self.display = display
        self._cache = {}
        self.today = date.today()
        # The user's chosen provider (Profile → Exchange rates) serves every new market lookup.
        self.provider, self.fallback = user_rate_settings(user)

    def _market_date(self, first):
        """Past month: its last day. Current or future month: today (latest rate)."""
        if (first.year, first.month) >= (self.today.year, self.today.month):
            return None
        return month_end(first)

    def rate(self, first, base, quote=None):
        """MonthlyRate for converting `base` into `quote` (default: display) in month `first`, or None."""
        quote = quote or self.display
        key = (first, base, quote)
        if key in self._cache:
            return self._cache[key]
        if base == quote:
            self._cache[key] = None
            return None

        row = MonthlyRate.objects.filter(user=self.user, month=first, base=base, quote=quote).first()
        current = self._market_date(first) is None
        # The current month's market rate is refreshed at most once a day (by when we fetched it: the
        # market's own date can lag a day). Past months and manual rates are never refetched.
        stale = row and row.source == "market" and current and row.updated_at.date() < self.today
        if row is None or stale:
            found = market_rate(base, quote, self._market_date(first), provider=self.provider, fallback=self.fallback)
            if found:
                row = self._store(first, base, quote, found, row)
        self._cache[key] = row
        return row

    def _store(self, first, base, quote, found, existing):
        values = {
            "rate": Decimal(found["rate"]).quantize(RATE_PLACES, rounding=ROUND_HALF_UP),
            "source": "market",
            "as_of": date.fromisoformat(str(found["date"])[:10]) if found.get("date") else self.today,
            "provider": found.get("provider") or "",
        }
        if existing is not None:
            # Only replace a market rate; a manual rate set meanwhile wins.
            MonthlyRate.objects.filter(pk=existing.pk, source="market").update(**values, updated_at=timezone.now())
            return MonthlyRate.objects.get(pk=existing.pk)
        try:
            with transaction.atomic():
                return MonthlyRate.objects.create(user=self.user, month=first, base=base, quote=quote, **values)
        except IntegrityError:
            # Another request stored this month's rate at the same moment: use theirs.
            return MonthlyRate.objects.get(user=self.user, month=first, base=base, quote=quote)

    def convert(self, amount, currency, first, quote=None):
        """(converted Decimal, MonthlyRate or None). Converted is None when no rate is available."""
        quote = quote or self.display
        if currency == quote:
            return to_cents(amount), None
        row = self.rate(first, currency, quote)
        if row is None:
            return None, None
        return to_cents(Decimal(amount) * row.rate), row


def rate_payload(row):
    return {
        "currency": row.base,
        "quote": row.quote,
        "rate": f"{row.rate:.8f}".rstrip("0").rstrip("."),
        "source": row.source,
        "as_of": row.as_of.isoformat() if row.as_of else None,
        "provider": row.provider or None,
    }


def refresh_market_rate(conv, row):
    """Re-fetch one stored *market* rate from the user's current provider. Returns (old_rate, row) or None.

    Manual rates are never touched. Past months use the month's last day, the current month the latest rate.
    """
    if row.source != "market":
        return None
    found = market_rate(row.base, row.quote, conv._market_date(row.month), provider=conv.provider, fallback=conv.fallback)
    if not found:
        return None
    old = row.rate
    conv._store(row.month, row.base, row.quote, found, row)
    row.refresh_from_db()
    return old, row

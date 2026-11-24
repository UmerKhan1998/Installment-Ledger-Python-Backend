"""Exchange-rate providers: one central place the whole app gets market rates from.

Each user picks a provider (Profile → Exchange rates). Plan payments (/api/rates/) and expense conversion
(expenses.conversion) both call `market_rate`, so changing the provider changes every *new* lookup.
Rates already stored (MonthlyRate, payments) never change on their own; they can be refreshed on request.

If the chosen provider can't answer (unsupported currency, date before its history, service down) and the user
allows fallback, the other providers are tried in order. Every result says which provider actually served it.
"""
import json
import urllib.request
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache

from .models import CURRENCY_CHOICES

SUPPORTED = {code for code, _ in CURRENCY_CHOICES}
LOOKBACK_DAYS = 5  # a day with no published rate falls back to the closest earlier day
USER_AGENT = "installment-ledger/1.0"


def _fetch_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


class Provider:
    key = ""
    name = ""
    website = ""
    description = ""
    history_from = None  # earliest date with history; None = latest rate only
    currencies = None    # set of supported codes; None = all of ours

    def supports(self, base, quote):
        return self.currencies is None or (base in self.currencies and quote in self.currencies)

    def has_day(self, day):
        return self.history_from is not None and day >= self.history_from

    def fetch(self, base, quote, tag):
        """Rate for `tag` ('latest' or 'YYYY-MM-DD') -> {"rate": Decimal, "date": 'YYYY-MM-DD'} or None."""
        raise NotImplementedError

    def info(self):
        return {
            "key": self.key,
            "name": self.name,
            "website": self.website,
            "description": self.description,
            "history_from": self.history_from.isoformat() if self.history_from else None,
            "currencies": sorted(self.currencies) if self.currencies else sorted(SUPPORTED),
            "missing_currencies": sorted(SUPPORTED - self.currencies) if self.currencies else [],
        }


class CurrencyApi(Provider):
    key = "currency_api"
    name = "Currency API (fawazahmed0)"
    website = "https://github.com/fawazahmed0/exchange-api"
    description = "Free, no key. Daily rates for 200+ currencies incl. PKR; history from March 2024."
    history_from = date(2024, 3, 2)
    MIRRORS = [
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{tag}/v1/currencies/{base}.min.json",
        "https://{tag}.currency-api.pages.dev/v1/currencies/{base}.min.json",
    ]

    def fetch(self, base, quote, tag):
        for template in self.MIRRORS:
            try:
                data = _fetch_json(template.format(tag=tag, base=base.lower()))
                value = data[base.lower()][quote.lower()]
                return {"rate": Decimal(str(value)), "date": str(data.get("date", tag))[:10]}
            except Exception:
                continue
        return None


class FxRatesApi(Provider):
    key = "fxratesapi"
    name = "FXRatesAPI"
    website = "https://fxratesapi.com"
    description = "Free tier without a key. Covers PKR with daily history going back several years."
    history_from = date(2015, 1, 1)

    def fetch(self, base, quote, tag):
        url = (f"https://api.fxratesapi.com/latest?base={base}&currencies={quote}" if tag == "latest"
               else f"https://api.fxratesapi.com/historical?date={tag}&base={base}&currencies={quote}")
        try:
            data = _fetch_json(url)
            if data.get("success") is False:
                return None
            return {"rate": Decimal(str(data["rates"][quote])), "date": str(data.get("date", tag))[:10]}
        except Exception:
            return None


class Frankfurter(Provider):
    key = "frankfurter"
    name = "Frankfurter (European Central Bank)"
    website = "https://frankfurter.dev"
    description = "Official ECB reference rates, history since 1999. Does not cover PKR, AED or SAR."
    history_from = date(1999, 1, 4)
    currencies = {"USD", "EUR", "GBP", "INR", "CAD"}

    def fetch(self, base, quote, tag):
        try:
            data = _fetch_json(f"https://api.frankfurter.dev/v1/{tag}?base={base}&symbols={quote}")
            return {"rate": Decimal(str(data["rates"][quote])), "date": str(data.get("date", tag))[:10]}
        except Exception:
            return None


class OpenErApi(Provider):
    key = "open_er_api"
    name = "ExchangeRate-API (open access)"
    website = "https://www.exchangerate-api.com/docs/free"
    description = "Free, no key, updated daily. Latest rates only (no history)."
    history_from = None

    def fetch(self, base, quote, tag):
        if tag != "latest":
            return None
        try:
            data = _fetch_json(f"https://open.er-api.com/v6/latest/{base}")
            if data.get("result") != "success":
                return None
            return {"rate": Decimal(str(data["rates"][quote])), "date": date.today().isoformat()}
        except Exception:
            return None


PROVIDERS = {p.key: p for p in (CurrencyApi(), FxRatesApi(), Frankfurter(), OpenErApi())}
DEFAULT_PROVIDER = "currency_api"
FALLBACK_ORDER = ["currency_api", "fxratesapi", "open_er_api", "frankfurter"]


def _cached(provider, base, quote, tag):
    key = f"fx:{provider.key}:{base}:{quote}:{tag}"
    hit = cache.get(key)
    if hit:
        return hit
    found = provider.fetch(base, quote, tag)
    if found:
        cache.set(key, found, 60 * 60 if tag == "latest" else 60 * 60 * 24 * 30)
    return found


def _from_provider(provider, base, quote, wanted, today):
    if not provider.supports(base, quote):
        return None
    if wanted is None or wanted >= today:
        return _cached(provider, base, quote, "latest")
    if not provider.has_day(wanted):
        return None
    for back in range(LOOKBACK_DAYS + 1):
        found = _cached(provider, base, quote, (wanted - timedelta(days=back)).isoformat())
        if found:
            return found
    return None


def market_rate(base, quote, wanted=None, provider=None, fallback=True):
    """Market rate {rate, date, source, provider} for `wanted` (a date; None/today/future = latest), or None.

    `provider` is a provider key (default: DEFAULT_PROVIDER). With `fallback`, the other providers are tried
    in FALLBACK_ORDER when the chosen one can't answer; `provider` in the result is the one that did.
    """
    if base == quote:
        return {"rate": Decimal("1"), "date": (wanted or date.today()).isoformat(), "source": None, "provider": None}
    today = date.today()
    chosen = provider if provider in PROVIDERS else DEFAULT_PROVIDER
    order = [chosen] + ([k for k in FALLBACK_ORDER if k != chosen] if fallback else [])
    for key in order:
        found = _from_provider(PROVIDERS[key], base, quote, wanted, today)
        if found:
            return {**found, "source": PROVIDERS[key].name, "provider": key}
    return None


def user_rate_settings(user):
    """(provider key, fallback) for a user; defaults when they never chose."""
    from .models import RateSettings

    if user is None or not getattr(user, "is_authenticated", False):
        return DEFAULT_PROVIDER, True
    prefs = RateSettings.objects.filter(user=user).first()
    if not prefs:
        return DEFAULT_PROVIDER, True
    return prefs.provider, prefs.fallback


def market_rate_for(user, base, quote, wanted=None):
    provider, fallback = user_rate_settings(user)
    return market_rate(base, quote, wanted, provider=provider, fallback=fallback)

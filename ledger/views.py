import json
import urllib.request
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache

from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CURRENCY_CHOICES, Payment, Plan
from .schedule import plan_figures
from .serializers import PaymentSerializer, PlanSerializer


def user_plans(request):
    return Plan.objects.filter(user=request.user).prefetch_related("payments")


class PlanListCreateView(generics.ListCreateAPIView):
    serializer_class = PlanSerializer
    pagination_class = None

    def get_queryset(self):
        return user_plans(self.request)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "with_detail": self.request.method == "POST"}


class PlanDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PlanSerializer

    def get_queryset(self):
        return user_plans(self.request)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "with_detail": True}


class PlanPaymentMixin:
    serializer_class = PaymentSerializer
    pagination_class = None

    def get_plan(self):
        if not hasattr(self, "_plan"):
            self._plan = get_object_or_404(user_plans(self.request), pk=self.kwargs["plan_id"])
        return self._plan

    def get_queryset(self):
        return Payment.objects.filter(plan=self.get_plan())

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "plan": self.get_plan()}


class PaymentListCreateView(PlanPaymentMixin, generics.ListCreateAPIView):
    def perform_create(self, serializer):
        plan = self.get_plan()
        latest = plan.payments.order_by("-paid_on").first()
        payment = serializer.save(plan=plan)
        # The newest transfer's rate becomes the plan's rate, so "remaining in PKR" tracks today's rate.
        if payment.exchange_rate and (latest is None or payment.paid_on >= latest.paid_on):
            if plan.exchange_rate != payment.exchange_rate:
                plan.exchange_rate = payment.exchange_rate
                plan.save(update_fields=["exchange_rate", "updated_at"])


class PaymentDetailView(PlanPaymentMixin, generics.RetrieveUpdateDestroyAPIView):
    pass


class SummaryView(APIView):
    """Totals across all of the user's plans, for the dashboard header."""

    def get(self, request):
        plans = list(user_plans(request))
        today = date.today()
        figures = [plan_figures(p, today) for p in plans]
        active = [f for f in figures if not f["is_complete"]]
        upcoming = [f["next_due_date"] for f in active if f["next_due_date"]]

        # Same figures in the currency the money is sent in (e.g. PKR), per payment currency.
        by_currency = {}
        for f in figures:
            conv = f["conversion"]
            if not conv:
                continue
            row = by_currency.setdefault(
                conv["currency"], {"currency": conv["currency"], "remaining": Decimal("0"), "monthly": Decimal("0"), "sent": Decimal("0"), "rates": set()}
            )
            row["remaining"] += conv["remaining_amount"]
            row["sent"] += conv["sent_total"]
            row["rates"].add(conv["rate"])
            if not f["is_complete"]:
                row["monthly"] += conv["monthly_amount"]
        payment_totals = [
            {
                "currency": r["currency"],
                "total_left": f"{r['remaining']:.2f}",
                "monthly_commitment": f"{r['monthly']:.2f}",
                "total_sent": f"{r['sent']:.2f}",
                "rate": f"{next(iter(r['rates'])):.4f}" if len(r["rates"]) == 1 else None,
            }
            for r in by_currency.values()
        ]
        return Response(
            {
                "plan_count": len(plans),
                "active_plan_count": len(active),
                "total_amount": f"{sum((p.total_amount for p in plans), Decimal('0')):.2f}",
                "total_paid": f"{sum((f['paid_amount'] for f in figures), Decimal('0')):.2f}",
                "total_left": f"{sum((f['remaining_amount'] for f in figures), Decimal('0')):.2f}",
                "monthly_commitment": f"{sum((f['monthly_amount'] for f in active), Decimal('0')):.2f}",
                "overdue_count": sum(f["overdue_count"] for f in figures),
                "next_due_date": min(upcoming).isoformat() if upcoming else None,
                "currency": plans[0].currency if plans else "USD",
                "payment_totals": payment_totals,
            }
        )


SUPPORTED = {code for code, _ in CURRENCY_CHOICES}

# Daily rates (incl. PKR) by date, from 2024-03 onwards; two mirrors of the same dataset.
DATED_SOURCES = [
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{tag}/v1/currencies/{base}.min.json",
    "https://{tag}.currency-api.pages.dev/v1/currencies/{base}.min.json",
]
LATEST_FALLBACK = "https://open.er-api.com/v6/latest/{base}"
HISTORY_START = date(2024, 3, 2)
LOOKBACK_DAYS = 5  # a missing day falls back to the closest earlier published day


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "installment-ledger/1.0"})
    with urllib.request.urlopen(req, timeout=6) as res:
        return json.load(res)


def _dated_rate(base, quote, tag):
    """Rate for one day (tag = 'YYYY-MM-DD' or 'latest'), or None if not published."""
    key = f"fx:{base}:{quote}:{tag}"
    cached = cache.get(key)
    if cached:
        return cached
    for template in DATED_SOURCES:
        try:
            data = _fetch_json(template.format(tag=tag, base=base.lower()))
            value = data[base.lower()][quote.lower()]
        except Exception:
            continue
        result = {"rate": Decimal(str(value)), "date": data.get("date", tag), "source": "fawazahmed0/currency-api"}
        cache.set(key, result, 60 * 60 if tag == "latest" else 60 * 60 * 24 * 30)
        return result
    return None


def _latest_rate(base, quote):
    result = _dated_rate(base, quote, "latest")
    if result:
        return result
    try:
        data = _fetch_json(LATEST_FALLBACK.format(base=base))
        value = data["rates"][quote]
    except Exception:
        return None
    return {"rate": Decimal(str(value)), "date": date.today().isoformat(), "source": "open.er-api.com"}


class LiveRateView(APIView):
    """GET /api/rates/?base=USD&quote=PKR[&date=YYYY-MM-DD] -> market rate for that day.

    No date, today or a future date -> latest rate. A past date -> that day's rate (or the
    closest earlier published day). Only a suggestion: each payment stores the rate the user saves.
    """

    def get(self, request):
        base = request.query_params.get("base", "USD").upper()
        quote = request.query_params.get("quote", "PKR").upper()
        if base not in SUPPORTED or quote not in SUPPORTED:
            return Response({"detail": "Unsupported currency."}, status=400)
        if base == quote:
            return Response({"base": base, "quote": quote, "rate": "1.0000", "date": None, "source": None})

        raw = request.query_params.get("date")
        try:
            wanted = date.fromisoformat(raw) if raw else None
        except ValueError:
            return Response({"detail": "date must be YYYY-MM-DD."}, status=400)

        today = date.today()
        if wanted is None or wanted >= today:
            result = _latest_rate(base, quote)
        elif wanted < HISTORY_START:
            return Response(
                {"detail": f"No market rates before {HISTORY_START:%b %d, %Y}. Enter the rate manually."}, status=404
            )
        else:
            result = None
            for back in range(LOOKBACK_DAYS + 1):
                result = _dated_rate(base, quote, (wanted - timedelta(days=back)).isoformat())
                if result:
                    break

        if not result:
            return Response({"detail": "Market rate is unavailable right now. Enter the rate manually."}, status=503)
        return Response(
            {
                "base": base,
                "quote": quote,
                "rate": f"{result['rate']:.4f}",
                "date": result["date"],
                "requested_date": wanted.isoformat() if wanted else None,
                "source": result["source"],
            }
        )

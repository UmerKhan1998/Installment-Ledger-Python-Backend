import json
import urllib.request
from datetime import date
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
RATE_SOURCE = "https://open.er-api.com/v6/latest/{base}"


class LiveRateView(APIView):
    """GET /api/rates/?base=USD&quote=PKR -> today's market rate (cached for an hour).

    Only a suggestion: the plan and each payment store the rate the user actually got.
    """

    def get(self, request):
        base = request.query_params.get("base", "USD").upper()
        quote = request.query_params.get("quote", "PKR").upper()
        if base not in SUPPORTED or quote not in SUPPORTED:
            return Response({"detail": "Unsupported currency."}, status=400)
        key = f"fx:{base}"
        data = cache.get(key)
        if data is None:
            try:
                with urllib.request.urlopen(RATE_SOURCE.format(base=base), timeout=6) as res:
                    data = json.load(res)
                if data.get("result") != "success":
                    raise ValueError("rate service error")
                cache.set(key, data, 60 * 60)
            except Exception:
                return Response({"detail": "Live rate is unavailable right now. Enter the rate manually."}, status=503)
        rate = data.get("rates", {}).get(quote)
        if rate is None:
            return Response({"detail": f"No rate for {quote}."}, status=404)
        return Response(
            {
                "base": base,
                "quote": quote,
                "rate": f"{Decimal(str(rate)):.4f}",
                "updated_at": data.get("time_last_update_utc"),
                "source": "open.er-api.com",
            }
        )

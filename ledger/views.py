from datetime import date
from decimal import Decimal


from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .duplicates import describe, find_duplicate, normalize_hash, normalize_reference
from . import receipt_storage
from .models import Payment, Plan, RateSettings
from .rates import PROVIDERS, SUPPORTED, market_rate, market_rate_for, user_rate_settings
from .schedule import plan_figures
from .serializers import PaymentSerializer, PlanSerializer


def user_plans(request):
    # Receipt images are only loaded by ReceiptView, never in lists.
    payments = Prefetch("payments", queryset=Payment.objects.defer("receipt_data"))
    return Plan.objects.filter(user=request.user).prefetch_related(payments)


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


class PaymentBulkDeleteView(PlanPaymentMixin, APIView):
    """POST /api/plans/{plan_id}/payments/bulk-delete/  {"ids": [1, 2, 3]}

    All-or-nothing: if any id is not a payment of this plan, nothing is deleted.
    """

    MAX_IDS = 500

    def post(self, request, plan_id):
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > self.MAX_IDS:
            return Response({"detail": f"Send 1 to {self.MAX_IDS} payment ids in \"ids\"."}, status=400)
        try:
            ids = {int(i) for i in ids}
        except (TypeError, ValueError):
            return Response({"detail": "Payment ids must be numbers."}, status=400)
        with transaction.atomic():
            payments = self.get_queryset().filter(pk__in=ids)
            found = set(payments.values_list("pk", flat=True))
            missing = sorted(ids - found)
            if missing:
                return Response({"detail": f"Payments not found in this plan: {missing}"}, status=404)
            deleted, _ = payments.delete()
        return Response({"deleted": deleted})


class DuplicateCheckView(APIView):
    """GET /api/payments/duplicate-check/?reference=972228&receipt_hash=<sha256>

    Lets the receipt scanner warn straight after OCR, before the form is filled.
    Saving still re-checks, so this is only an early warning.
    """

    def get(self, request):
        reference = normalize_reference(request.query_params.get("reference"))
        receipt_hash = normalize_hash(request.query_params.get("receipt_hash"))
        match, reason = find_duplicate(request.user, reference=reference, receipt_hash=receipt_hash)
        if not match:
            return Response({"duplicate": False})
        return Response(
            {
                "duplicate": True,
                "reason": reason,
                "message": f"Already recorded ({reason}): {describe(match)}.",
                "payment": {"id": match.id, "plan_id": match.plan_id, "plan_item": match.plan.item, "paid_on": match.paid_on},
            }
        )


class ReceiptView(PlanPaymentMixin, APIView):
    """GET /api/plans/{plan_id}/payments/{pk}/receipt/ -> the uploaded receipt image."""

    def get(self, request, plan_id, pk):
        payment = get_object_or_404(self.get_queryset(), pk=pk)
        if not payment.receipt_type:
            raise Http404
        if payment.receipt_path:
            try:
                data = receipt_storage.load(payment.receipt_path)
            except Exception:
                return Response({"detail": "The receipt file couldn't be loaded from storage right now."}, status=502)
        elif payment.receipt_data is not None:
            data = bytes(payment.receipt_data)
        else:
            raise Http404
        response = HttpResponse(data, content_type=payment.receipt_type)
        response["Content-Disposition"] = f'inline; filename="{payment.receipt_name or "receipt"}"'
        response["Cache-Control"] = "private, max-age=3600"
        return response


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

        result = market_rate_for(request.user, base, quote, wanted)

        if not result:
            provider, _ = user_rate_settings(request.user)
            name = PROVIDERS[provider].name if provider in PROVIDERS else "the provider"
            return Response(
                {"detail": f"No market rate from {name} for {base}→{quote}{' on ' + wanted.isoformat() if wanted else ''}. "
                           "Enter the rate manually, or pick another provider in Profile → Exchange rates."},
                status=404 if wanted else 503,
            )
        return Response(
            {
                "base": base,
                "quote": quote,
                "rate": f"{result['rate']:.4f}",
                "date": result["date"],
                "requested_date": wanted.isoformat() if wanted else None,
                "source": result["source"],
                "provider": result["provider"],
            }
        )


class RateProvidersView(APIView):
    """GET /api/rates/providers/ -> available providers, with the user's choice."""

    def get(self, request):
        provider, fallback = user_rate_settings(request.user)
        return Response({"selected": provider, "fallback": fallback, "providers": [p.info() for p in PROVIDERS.values()]})


class RateSettingsView(APIView):
    """GET / PATCH /api/rates/settings/ {"provider": "fxratesapi", "fallback": true}"""

    def get(self, request):
        provider, fallback = user_rate_settings(request.user)
        return Response({"provider": provider, "fallback": fallback})

    def patch(self, request):
        prefs, _ = RateSettings.objects.get_or_create(user=request.user)
        if "provider" in request.data:
            if request.data["provider"] not in PROVIDERS:
                return Response({"provider": [f"Use one of: {', '.join(PROVIDERS)}."]}, status=400)
            prefs.provider = request.data["provider"]
        if "fallback" in request.data:
            prefs.fallback = bool(request.data["fallback"])
        prefs.save()
        return Response({"provider": prefs.provider, "fallback": prefs.fallback})


class RateTestView(APIView):
    """GET /api/rates/test/?provider=fxratesapi&base=USD&quote=PKR[&date=YYYY-MM-DD]

    Asks one provider directly (no fallback), for the Profile page's "Test" button.
    """

    def get(self, request):
        key = request.query_params.get("provider", "")
        base = request.query_params.get("base", "USD").upper()
        quote = request.query_params.get("quote", "PKR").upper()
        if key not in PROVIDERS:
            return Response({"detail": "Unknown provider."}, status=400)
        if base not in SUPPORTED or quote not in SUPPORTED:
            return Response({"detail": "Unsupported currency."}, status=400)
        raw = request.query_params.get("date")
        try:
            wanted = date.fromisoformat(raw) if raw else None
        except ValueError:
            return Response({"detail": "date must be YYYY-MM-DD."}, status=400)
        provider = PROVIDERS[key]
        if not provider.supports(base, quote):
            return Response({"ok": False, "detail": f"{provider.name} doesn't cover {base}→{quote}."})
        found = market_rate(base, quote, wanted, provider=key, fallback=False)
        if not found:
            return Response({"ok": False, "detail": f"{provider.name} returned no rate for {base}→{quote}"
                                                    f"{' on ' + wanted.isoformat() if wanted else ''}."})
        return Response({"ok": True, "base": base, "quote": quote, "rate": f"{found['rate']:.8f}".rstrip("0").rstrip("."),
                         "date": found["date"], "provider": key})


class PlanExportView(APIView):
    """GET /api/plans/export/?ids=1,2&as=pdf|xlsx -> the chosen plans as a PDF statement or an Excel workbook.
    Only the signed-in user's plans can be exported; any id that isn't theirs makes the whole request 404."""

    FORMATS = {"pdf": ("application/pdf", "pdf"),
               "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")}

    def get(self, request):
        from .export import filename, to_pdf, to_xlsx

        fmt = request.query_params.get("as", "pdf")  # not "format": DRF reserves it for renderer negotiation
        if fmt not in self.FORMATS:
            return Response({"detail": "as must be pdf or xlsx"}, status=400)
        try:
            ids = list(dict.fromkeys(int(x) for x in request.query_params.get("ids", "").split(",") if x.strip()))
        except ValueError:
            return Response({"detail": "ids must be plan ids separated by commas"}, status=400)
        if not ids or len(ids) > 50:
            return Response({"detail": "Choose between 1 and 50 plans."}, status=400)
        found = {p.id: p for p in Plan.objects.filter(user=request.user, id__in=ids).prefetch_related("payments")}
        if len(found) != len(ids):
            return Response({"detail": "Plan not found."}, status=404)
        plans = [found[i] for i in ids]  # keep the order the user chose
        content_type, ext = self.FORMATS[fmt]
        body = to_pdf(plans) if fmt == "pdf" else to_xlsx(plans)
        res = HttpResponse(body, content_type=content_type)
        res["Content-Disposition"] = f'attachment; filename="{filename(plans, ext, date.today())}"'
        res["Cache-Control"] = "no-store"
        return res

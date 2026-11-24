"""Monthly expenses API: list / add / edit / delete, monthly summary, settings, copy recurring."""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from django.db.models import Prefetch

from ledger.models import CURRENCY_CHOICES, Payment, Plan

from .conversion import Converter, rate_payload, to_cents
from .plans import INSTALLMENTS, INSTALLMENTS_LABEL, due_in_month, linked_payments, linked_plan_ids
from .models import CATEGORY_CHOICES, Expense, ExpenseSettings, MonthlyRate
from .serializers import ExpenseSerializer, ExpenseSettingsSerializer

TREND_MONTHS = 6


def settings_for(user):
    obj, _ = ExpenseSettings.objects.get_or_create(user=user)
    return obj


def parse_month(raw):
    """'2026-11' -> date(2026, 11, 1); missing -> this month."""
    if not raw:
        today = date.today()
        return today.replace(day=1)
    try:
        year, month = (int(x) for x in raw.split("-"))
        return date(year, month, 1)
    except (ValueError, TypeError):
        raise ValidationError({"month": "Use YYYY-MM, e.g. 2026-11."})


def shift_month(first, months):
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_range(first):
    return first, shift_month(first, 1)


def money(value):
    return f"{(value or Decimal('0')):.2f}"


class ExpenseSettingsView(generics.RetrieveUpdateAPIView):
    """Changing the currency never rewrites the budget; a newly entered budget is in the tracker currency."""

    serializer_class = ExpenseSettingsSerializer

    def get_object(self):
        return settings_for(self.request.user)

    def perform_update(self, serializer):
        extra = {}
        if "monthly_budget" in serializer.validated_data:
            cur = serializer.validated_data.get("currency", serializer.instance.currency)
            extra["budget_currency"] = cur if serializer.validated_data["monthly_budget"] is not None else ""
        serializer.save(**extra)


class ExpenseListCreateView(generics.ListCreateAPIView):
    """GET /api/expenses/?month=2026-11[&category=food][&currency=PKR]"""

    serializer_class = ExpenseSerializer
    pagination_class = None

    def get_queryset(self):
        qs = Expense.objects.filter(user=self.request.user)
        if self.request.method == "GET":
            start, end = month_range(parse_month(self.request.query_params.get("month")))
            qs = qs.filter(spent_on__gte=start, spent_on__lt=end)
            for field in ("category", "currency"):
                value = self.request.query_params.get(field)
                if value:
                    qs = qs.filter(**{field: value})
        return qs

    def perform_create(self, serializer):
        currency = serializer.validated_data.get("currency") or settings_for(self.request.user).currency
        serializer.save(user=self.request.user, currency=currency)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.user.is_authenticated:
            ctx["converter"] = Converter(self.request.user, settings_for(self.request.user).currency)
        return ctx


class ExpenseDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ExpenseSerializer

    def get_queryset(self):
        return Expense.objects.filter(user=self.request.user)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.user.is_authenticated:
            ctx["converter"] = Converter(self.request.user, settings_for(self.request.user).currency)
        return ctx


class ExpenseBulkDeleteView(APIView):
    """POST /api/expenses/bulk-delete/ {"ids": [...]}: all-or-nothing."""

    def post(self, request):
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not 0 < len(ids) <= 500:
            return Response({"detail": 'Send 1 to 500 expense ids in "ids".'}, status=400)
        try:
            ids = {int(i) for i in ids}
        except (TypeError, ValueError):
            return Response({"detail": "Expense ids must be numbers."}, status=400)
        with transaction.atomic():
            qs = Expense.objects.filter(user=request.user, pk__in=ids)
            missing = sorted(ids - set(qs.values_list("pk", flat=True)))
            if missing:
                return Response({"detail": f"Expenses not found: {missing}"}, status=404)
            deleted, _ = qs.delete()
        return Response({"deleted": deleted})


class ExpenseSummaryView(APIView):
    """GET /api/expenses/summary/?month=2026-11

    Everything is shown in the user's tracker currency. Expenses in other currencies are converted with the
    month's exchange rate (see MonthlyRate) and included. Only an expense whose rate can't be found at all
    is left out, and listed under `unconverted`.
    """

    def get(self, request):
        prefs = settings_for(request.user)
        cur = prefs.currency
        conv = Converter(request.user, cur)
        first = parse_month(request.query_params.get("month"))
        start, end = month_range(first)
        mine = Expense.objects.filter(user=request.user)

        # This month, row by row (each rounded to the cent, so the list adds up to the total).
        # Rows are the user's expenses plus payments of linked installment plans.
        labels = {**dict(CATEGORY_CHOICES), INSTALLMENTS: INSTALLMENTS_LABEL}
        plan_ids = linked_plan_ids(request.user, prefs)
        plan_rows = linked_payments(request.user, plan_ids, start, end)
        items = [
            (e.amount, e.currency, e.category, None)
            for e in mine.filter(spent_on__gte=start, spent_on__lt=end).only("amount", "currency", "category")
        ] + [(r["amount"], r["currency"], INSTALLMENTS, r) for r in plan_rows]
        total = Decimal("0")
        count = 0
        cats = {}
        unconverted = {}
        rates_used = {}
        plan_paid = {}
        for amount, currency, category, plan_row in items:
            value, rate_row = conv.convert(amount, currency, first)
            if plan_row is not None:
                plan_row["converted"] = None if currency == cur else (
                    {"currency": cur, "amount": money(value), "rate": f"{rate_row.rate:.8f}".rstrip("0").rstrip("."),
                     "source": rate_row.source} if value is not None else {"currency": cur, "amount": None, "rate": None, "source": None}
                )
            if value is None:
                u = unconverted.setdefault(currency, {"currency": currency, "total": Decimal("0"), "count": 0})
                u["total"] += amount
                u["count"] += 1
                continue
            if rate_row:
                rates_used[rate_row.base] = rate_row
            total += value
            count += 1
            if plan_row is not None:
                plan_paid[plan_row["plan_id"]] = plan_paid.get(plan_row["plan_id"], Decimal("0")) + value
            c = cats.setdefault(category, {"total": Decimal("0"), "count": 0, "foreign": 0})
            c["total"] += value
            c["count"] += 1
            c["foreign"] += 1 if rate_row else 0
        by_category = [
            {"category": k, "label": labels.get(k, k), "total": money(v["total"]), "count": v["count"],
             "converted_count": v["foreign"],
             "share": float(round(v["total"] / total * 100, 1)) if total else 0.0}
            for k, v in sorted(cats.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ]

        # Trend: same rule for each of the last months, each with its own monthly rate.
        trend_start = shift_month(first, -(TREND_MONTHS - 1))
        per_month = {}
        trend_rows = list(mine.filter(spent_on__gte=trend_start, spent_on__lt=end).values_list("amount", "currency", "spent_on"))
        trend_rows += [(r["amount"], r["currency"], r["spent_on"]) for r in linked_payments(request.user, plan_ids, trend_start, end)]
        for amount, currency, spent_on in trend_rows:
            m = spent_on.replace(day=1)
            value, _ = conv.convert(amount, currency, m)
            if value is not None:
                per_month[m] = per_month.get(m, Decimal("0")) + value
        trend = [
            {"month": shift_month(trend_start, i).strftime("%Y-%m"), "total": money(per_month.get(shift_month(trend_start, i)))}
            for i in range(TREND_MONTHS)
        ]
        previous = per_month.get(shift_month(first, -1), Decimal("0"))

        # Recurring items from last month that are not in this month yet (matched by title + category).
        prev_start, prev_end = month_range(shift_month(first, -1))
        here = {(t.lower(), c) for t, c in mine.filter(spent_on__gte=start, spent_on__lt=end).values_list("title", "category")}
        recurring_pending = sum(
            1 for t, c in mine.filter(is_recurring=True, spent_on__gte=prev_start, spent_on__lt=prev_end)
            .values_list("title", "category") if (t.lower(), c) not in here
        )

        # Budget as entered, converted for display at this month's rate if it was set in another currency.
        budget = prefs.monthly_budget
        budget_rate = None
        if budget is not None and prefs.budget_currency and prefs.budget_currency != cur:
            budget, budget_rate = conv.convert(budget, prefs.budget_currency, first)
        days_in_month = (end - start).days
        today = date.today()
        elapsed = days_in_month if end <= today else max((today - start).days + 1, 0) if start <= today else 0
        return Response({
            "month": first.strftime("%Y-%m"),
            "currency": cur,
            "total": money(total),
            "count": count,
            "budget": money(budget) if budget is not None else None,
            "remaining": money(budget - total) if budget is not None else None,
            "budget_used_percent": float(round(total / budget * 100, 1)) if budget else None,
            "budget_original": (
                {"amount": money(prefs.monthly_budget), "currency": prefs.budget_currency,
                 "rate": f"{budget_rate.rate:.8f}".rstrip("0").rstrip(".")} if budget_rate else None
            ),
            "daily_average": money(to_cents(total / elapsed)) if elapsed else money(0),
            "previous_month_total": money(previous),
            "change_percent": float(round((total - previous) / previous * 100, 1)) if previous else None,
            "by_category": by_category,
            "rates": [rate_payload(r) for r in sorted(rates_used.values(), key=lambda r: r.base)],
            "unconverted": [{**u, "total": money(u["total"])} for u in unconverted.values()],
            "trend": trend,
            "recurring_pending": recurring_pending,
            "linked_plans": self._linked_plans(request.user, plan_ids, conv, first, start, end, plan_paid),
            "plan_payments": [
                {**{k: v for k, v in r.items() if k not in ("amount", "spent_on", "paid_time")},
                 "amount": money(r["amount"]), "spent_on": r["spent_on"].isoformat(),
                 "paid_time": r["paid_time"].strftime("%H:%M") if r["paid_time"] else None,
                 "category": INSTALLMENTS, "category_label": INSTALLMENTS_LABEL}
                for r in plan_rows
            ],
        })

    @staticmethod
    def _linked_plans(user, plan_ids, conv, first, start, end, plan_paid):
        """One card per linked plan: paid this month (tracker currency) and what is due this month."""
        cards = []
        for plan in Plan.objects.filter(id__in=plan_ids, user=user).prefetch_related(
            Prefetch("payments", queryset=Payment.objects.defer("receipt_data"))
        ).order_by("item"):
            due = due_in_month(plan, start, end)
            left_conv, _ = conv.convert(due["left"], plan.currency, first)
            cards.append({
                "plan_id": plan.id,
                "item": plan.item,
                "plan_currency": plan.currency,
                "paid_this_month": money(plan_paid.get(plan.id, Decimal("0"))),
                "due_numbers": due["numbers"],
                "due_amount": money(due["amount"]),
                "due_left": money(due["left"]),
                "due_left_converted": money(left_conv) if left_conv is not None and plan.currency != conv.display else None,
                "is_complete": due["is_complete"],
            })
        return cards


class MonthRatesView(APIView):
    """Exchange rates used for a month (into the tracker currency).

    GET    /api/expenses/rates/?month=2026-11[&currency=USD]  -> rates for the month's foreign currencies
                                                                 (or one currency, e.g. for the add form)
    PUT    /api/expenses/rates/  {"month", "currency", "rate"} -> use your own rate for that month
    DELETE /api/expenses/rates/?month=2026-11&currency=USD    -> back to the market rate
    """

    SUPPORTED = {code for code, _ in CURRENCY_CHOICES}

    def _currency(self, raw):
        code = (raw or "").upper()
        if code not in self.SUPPORTED:
            raise ValidationError({"currency": "Unsupported currency."})
        return code

    def get(self, request):
        cur = settings_for(request.user).currency
        first = parse_month(request.query_params.get("month"))
        conv = Converter(request.user, cur)
        if request.query_params.get("currency"):
            wanted = [self._currency(request.query_params["currency"])]
        else:
            start, end = month_range(first)
            wanted = sorted(set(
                Expense.objects.filter(user=request.user, spent_on__gte=start, spent_on__lt=end)
                .exclude(currency=cur).values_list("currency", flat=True)
            ))
        rows, missing = [], []
        for code in wanted:
            if code == cur:
                continue
            row = conv.rate(first, code)
            (rows.append(rate_payload(row)) if row else missing.append(code))
        return Response({"month": first.strftime("%Y-%m"), "currency": cur, "rates": rows, "missing": missing})

    def put(self, request):
        cur = settings_for(request.user).currency
        first = parse_month(request.data.get("month"))
        code = self._currency(request.data.get("currency"))
        if code == cur:
            raise ValidationError({"currency": f"{cur} is the tracker currency; it needs no rate."})
        try:
            rate = Decimal(str(request.data.get("rate")))
        except Exception:
            raise ValidationError({"rate": "Enter a number."})
        if not rate.is_finite() or rate <= 0:
            raise ValidationError({"rate": "The rate must be more than 0."})
        row, _ = MonthlyRate.objects.update_or_create(
            user=request.user, month=first, base=code, quote=cur,
            defaults={"rate": rate.quantize(Decimal("0.00000001")), "source": "manual", "as_of": None},
        )
        return Response(rate_payload(row))

    def delete(self, request):
        cur = settings_for(request.user).currency
        first = parse_month(request.query_params.get("month"))
        code = self._currency(request.query_params.get("currency"))
        MonthlyRate.objects.filter(user=request.user, month=first, base=code, quote=cur).delete()
        row = Converter(request.user, cur).rate(first, code)  # re-fetch the market rate
        return Response(rate_payload(row) if row else {"currency": code, "rate": None})


class CopyRecurringView(APIView):
    """POST /api/expenses/copy-recurring/ {"month": "2026-11"}

    Copies last month's recurring expenses into `month` (same day, clamped to the month's length),
    skipping any already there (same title + category). Returns the new expenses.
    """

    def post(self, request):
        first = parse_month(request.data.get("month"))
        start, end = month_range(first)
        prev_start, prev_end = month_range(shift_month(first, -1))
        mine = Expense.objects.filter(user=request.user)
        here = {(t.lower(), c) for t, c in mine.filter(spent_on__gte=start, spent_on__lt=end).values_list("title", "category")}
        last_day = (end - start).days
        created = []
        with transaction.atomic():
            for old in mine.filter(is_recurring=True, spent_on__gte=prev_start, spent_on__lt=prev_end).order_by("spent_on"):
                key = (old.title.lower(), old.category)
                if key in here:
                    continue
                here.add(key)
                created.append(Expense.objects.create(
                    user=request.user, title=old.title, amount=old.amount, currency=old.currency,
                    category=old.category, note=old.note, is_recurring=True,
                    spent_on=first.replace(day=min(old.spent_on.day, last_day)),
                ))
        return Response(ExpenseSerializer(created, many=True).data, status=status.HTTP_201_CREATED)


class CategoriesView(APIView):
    def get(self, request):
        return Response([{"value": v, "label": label} for v, label in CATEGORY_CHOICES])

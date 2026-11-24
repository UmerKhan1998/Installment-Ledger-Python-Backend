"""Projects: separate expense books (wedding, Qurbani cow, trip...). Kept out of monthly expenses entirely.

Amounts are converted into the project's currency with the same stored monthly rates as monthly expenses
(each row rounded to the cent; totals are sums of rows, so the list always adds up).
"""
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ledger.models import CURRENCY_CHOICES

from .conversion import Converter
from .models import PROJECT_TEMPLATES, Project, ProjectExpense


def money(v):
    return f"{(v or Decimal('0')):.2f}"


def clean_categories(values):
    seen, out = set(), []
    for v in values or []:
        name = str(v).strip()[:60]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out[:40]


def project_figures(project, conv=None):
    """Totals for one project, in its currency. Returns (figures dict, {expense_id: converted payload})."""
    conv = conv or Converter(project.user, project.currency)
    total = Decimal("0")
    cats, months, unconverted, converted = {}, {}, {}, {}
    last = None
    count = 0
    for e in project.expenses.all():
        value, row = conv.convert(e.amount, e.currency, e.spent_on.replace(day=1))
        if e.currency != project.currency:
            converted[e.id] = (
                {"currency": project.currency, "amount": money(value), "rate": f"{row.rate:.8f}".rstrip("0").rstrip("."), "source": row.source}
                if value is not None else {"currency": project.currency, "amount": None, "rate": None, "source": None}
            )
        if value is None:
            u = unconverted.setdefault(e.currency, {"currency": e.currency, "total": Decimal("0"), "count": 0})
            u["total"] += e.amount
            u["count"] += 1
            continue
        total += value
        count += 1
        c = cats.setdefault(e.category, {"total": Decimal("0"), "count": 0})
        c["total"] += value
        c["count"] += 1
        key = e.spent_on.strftime("%Y-%m")
        months[key] = months.get(key, Decimal("0")) + value
        last = max(last, e.spent_on) if last else e.spent_on
    budget = project.budget
    figures = {
        "spent": money(total),
        "count": count,
        "remaining": money(budget - total) if budget is not None else None,
        "budget_used_percent": float(round(total / budget * 100, 1)) if budget else None,
        "last_expense_on": last.isoformat() if last else None,
        "by_category": [
            {"category": k, "total": money(v["total"]), "count": v["count"],
             "share": float(round(v["total"] / total * 100, 1)) if total else 0.0}
            for k, v in sorted(cats.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ],
        "by_month": [{"month": k, "total": money(v)} for k, v in sorted(months.items())],
        "unconverted": [{**u, "total": money(u["total"])} for u in unconverted.values()],
    }
    return figures, converted


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name", "template", "currency", "budget", "start_date", "end_date", "note",
                  "categories", "status", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")
        extra_kwargs = {"currency": {"required": False}, "categories": {"required": False}}

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the project a name.")
        return value

    def validate_template(self, value):
        if value not in PROJECT_TEMPLATES:
            raise serializers.ValidationError(f"Use one of: {', '.join(PROJECT_TEMPLATES)}.")
        return value

    def validate_categories(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Send a list of names.")
        return clean_categories(value)

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "The end date is before the start date."})
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        figures, _ = project_figures(instance, self.context.get("converter"))
        data.update(figures)
        return data


class ProjectExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectExpense
        fields = ("id", "title", "amount", "currency", "category", "spent_on", "note", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")
        extra_kwargs = {"currency": {"required": False}, "category": {"required": False}}

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter what the expense was for.")
        return value

    def validate_category(self, value):
        return (value or "").strip()[:60] or "Other"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        converted = self.context.get("converted")
        if converted is not None:
            data["converted"] = converted.get(instance.id)
        else:
            project = instance.project
            if instance.currency == project.currency:
                data["converted"] = None
            else:
                conv = Converter(project.user, project.currency)
                value, row = conv.convert(instance.amount, instance.currency, instance.spent_on.replace(day=1))
                data["converted"] = {"currency": project.currency, "amount": money(value) if value is not None else None,
                                     "rate": f"{row.rate:.8f}".rstrip("0").rstrip(".") if row else None,
                                     "source": row.source if row else None}
        return data


def user_projects(request):
    return Project.objects.filter(user=request.user).prefetch_related("expenses")


class ProjectListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    pagination_class = None

    def get_queryset(self):
        return user_projects(self.request)

    def perform_create(self, serializer):
        from .views import settings_for

        template = serializer.validated_data.get("template", "custom")
        categories = serializer.validated_data.get("categories") or list(PROJECT_TEMPLATES[template])
        currency = serializer.validated_data.get("currency") or settings_for(self.request.user).currency
        serializer.save(user=self.request.user, categories=categories, currency=currency)


class ProjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProjectSerializer

    def get_queryset(self):
        return user_projects(self.request)


class ProjectExpenseMixin:
    serializer_class = ProjectExpenseSerializer
    pagination_class = None

    def get_project(self):
        if not hasattr(self, "_project"):
            self._project = get_object_or_404(user_projects(self.request), pk=self.kwargs["project_id"])
        return self._project

    def get_queryset(self):
        return ProjectExpense.objects.filter(project=self.get_project())

    def remember_category(self, category):
        project = self.get_project()
        if category and category.lower() not in {c.lower() for c in project.categories}:
            project.categories = clean_categories([*project.categories, category])
            project.save(update_fields=["categories", "updated_at"])


class ProjectExpenseListCreateView(ProjectExpenseMixin, generics.ListCreateAPIView):
    def list(self, request, *args, **kwargs):
        project = self.get_project()
        _, converted = project_figures(project)
        data = ProjectExpenseSerializer(project.expenses.all(), many=True, context={"converted": converted}).data
        return Response(data)

    def perform_create(self, serializer):
        project = self.get_project()
        instance = serializer.save(project=project, currency=serializer.validated_data.get("currency") or project.currency)
        self.remember_category(instance.category)


class ProjectExpenseDetailView(ProjectExpenseMixin, generics.RetrieveUpdateDestroyAPIView):
    def perform_update(self, serializer):
        instance = serializer.save()
        self.remember_category(instance.category)


class ProjectExpenseBulkDeleteView(ProjectExpenseMixin, APIView):
    def post(self, request, project_id):
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not 0 < len(ids) <= 500:
            return Response({"detail": 'Send 1 to 500 expense ids in "ids".'}, status=400)
        try:
            ids = {int(i) for i in ids}
        except (TypeError, ValueError):
            return Response({"detail": "Expense ids must be numbers."}, status=400)
        with transaction.atomic():
            qs = self.get_queryset().filter(pk__in=ids)
            missing = sorted(ids - set(qs.values_list("pk", flat=True)))
            if missing:
                return Response({"detail": f"Expenses not found in this project: {missing}"}, status=404)
            deleted, _ = qs.delete()
        return Response({"deleted": deleted})


class ProjectTemplatesView(APIView):
    def get(self, request):
        return Response([{"value": k, "categories": v} for k, v in PROJECT_TEMPLATES.items()])

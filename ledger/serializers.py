from decimal import Decimal

from rest_framework import serializers

from .models import Payment, Plan
from .schedule import plan_figures


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ("id", "amount", "paid_on", "note", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_amount(self, value):
        plan = self.context["plan"]
        already_paid = sum((p.amount for p in plan.payments.all() if p.pk != getattr(self.instance, "pk", None)), Decimal("0"))
        remaining = plan.total_amount - already_paid
        if value > remaining:
            raise serializers.ValidationError(f"Amount is more than what is left to pay ({remaining:.2f}).")
        return value


class ScheduleRowSerializer(serializers.Serializer):
    number = serializers.IntegerField()
    due_date = serializers.DateField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    status = serializers.CharField()


class PlanSerializer(serializers.ModelSerializer):
    """Plan fields plus figures derived from its payments (all read-only)."""

    class Meta:
        model = Plan
        fields = (
            "id", "item", "purchase_price", "total_amount", "months",
            "first_payment_date", "notes", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_total_amount(self, value):
        if self.instance:
            paid = sum((p.amount for p in self.instance.payments.all()), Decimal("0"))
            if value < paid:
                raise serializers.ValidationError(f"Total cannot be less than what is already paid ({paid:.2f}).")
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        figures = plan_figures(instance)
        schedule = figures.pop("schedule")
        money = serializers.DecimalField(max_digits=12, decimal_places=2)
        for key in ("monthly_amount", "paid_amount", "remaining_amount", "next_due_amount"):
            figures[key] = money.to_representation(figures[key])
        for key in ("next_due_date", "final_due_date"):
            figures[key] = figures[key].isoformat() if figures[key] else None
        data.update(figures)
        if self.context.get("with_detail"):
            data["schedule"] = ScheduleRowSerializer(schedule, many=True).data
            data["payments"] = PaymentSerializer(instance.payments.all(), many=True).data
        return data

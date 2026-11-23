from decimal import Decimal

from rest_framework import serializers

from .models import Payment, Plan
from .schedule import plan_figures, to_cents

MONEY = serializers.DecimalField(max_digits=14, decimal_places=2)
RATE = serializers.DecimalField(max_digits=14, decimal_places=4)


class PaymentSerializer(serializers.ModelSerializer):
    """Send either `amount` (plan currency), or `sent_amount` (+ optional `exchange_rate`,
    `sent_currency`) and the plan-currency amount is worked out: sent_amount / rate."""

    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False)
    sent_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True
    )
    exchange_rate = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True
    )

    class Meta:
        model = Payment
        fields = ("id", "amount", "sent_amount", "sent_currency", "exchange_rate", "paid_on", "note", "created_at")
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        plan = self.context["plan"]
        sent = attrs.get("sent_amount")
        converted = False

        if sent is not None:
            currency = attrs.get("sent_currency") or plan.payment_currency
            if currency == plan.currency:
                attrs.update(amount=sent, sent_amount=None, sent_currency="", exchange_rate=None)
            else:
                if currency != plan.payment_currency:
                    raise serializers.ValidationError(
                        {"sent_currency": f"This plan is paid in {plan.payment_currency} or {plan.currency}."}
                    )
                rate = attrs.get("exchange_rate") or plan.exchange_rate
                attrs.update(amount=to_cents(sent / rate), sent_currency=currency, exchange_rate=rate)
                converted = True
        elif "amount" in attrs:
            attrs.update(sent_amount=None, sent_currency="", exchange_rate=None)
        elif not self.instance:
            raise serializers.ValidationError({"amount": "Enter an amount."})

        if "amount" in attrs:
            others = sum((p.amount for p in plan.payments.all() if p.pk != getattr(self.instance, "pk", None)), Decimal("0"))
            remaining = plan.total_amount - others
            if attrs["amount"] > remaining:
                # A converted payment can land a cent over because of rounding: treat it as settling the plan.
                if converted and attrs["amount"] - remaining <= Decimal("0.01"):
                    attrs["amount"] = remaining
                else:
                    raise serializers.ValidationError(
                        {"amount": f"That is {attrs['amount']:.2f} {plan.currency}, more than what is left to pay ({remaining:.2f})."}
                    )
        return attrs


class ScheduleRowSerializer(serializers.Serializer):
    number = serializers.IntegerField()
    due_date = serializers.DateField()
    amount = MONEY
    paid_amount = MONEY
    status = serializers.CharField()


class PlanSerializer(serializers.ModelSerializer):
    """Plan fields plus figures derived from its payments (all read-only)."""

    class Meta:
        model = Plan
        fields = (
            "id", "item", "purchase_price", "total_amount", "months", "first_payment_date",
            "currency", "payment_currency", "exchange_rate", "notes", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_total_amount(self, value):
        if self.instance:
            paid = sum((p.amount for p in self.instance.payments.all()), Decimal("0"))
            if value < paid:
                raise serializers.ValidationError(f"Total cannot be less than what is already paid ({paid:.2f}).")
        return value

    def validate(self, attrs):
        currency = attrs.get("currency", getattr(self.instance, "currency", "USD"))
        pay_currency = attrs.get("payment_currency", getattr(self.instance, "payment_currency", currency))
        if pay_currency == currency:
            attrs["exchange_rate"] = Decimal("1")
        elif "exchange_rate" not in attrs and (not self.instance or self.instance.exchange_rate == 1):
            raise serializers.ValidationError({"exchange_rate": f"Enter how many {pay_currency} make 1 {currency}."})
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        figures = plan_figures(instance)
        schedule = figures.pop("schedule")
        conversion = figures.pop("conversion")
        for key in ("monthly_amount", "paid_amount", "remaining_amount", "next_due_amount"):
            figures[key] = MONEY.to_representation(figures[key])
        for key in ("next_due_date", "final_due_date"):
            figures[key] = figures[key].isoformat() if figures[key] else None
        if conversion:
            conversion = {
                k: (RATE.to_representation(v) if k == "rate" else v if k == "currency" else MONEY.to_representation(v))
                for k, v in conversion.items()
            }
        data.update(figures, conversion=conversion)
        if self.context.get("with_detail"):
            data["schedule"] = ScheduleRowSerializer(schedule, many=True).data
            data["payments"] = PaymentSerializer(instance.payments.all(), many=True).data
        return data

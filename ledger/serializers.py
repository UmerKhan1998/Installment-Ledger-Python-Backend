from decimal import Decimal

from rest_framework import serializers

from .duplicates import describe, file_hash, find_duplicate, normalize_hash, normalize_reference
from .models import Payment, Plan
from .schedule import plan_figures, to_cents

MONEY = serializers.DecimalField(max_digits=14, decimal_places=2)
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
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

    receipt = serializers.ImageField(write_only=True, required=False, allow_null=True)
    has_receipt = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = (
            "id", "amount", "sent_amount", "sent_currency", "exchange_rate", "paid_on", "paid_time",
            "note", "reference", "receipt_hash", "receipt", "has_receipt", "created_at",
        )
        extra_kwargs = {"receipt_hash": {"write_only": True}}
        read_only_fields = ("id", "created_at")

    def get_has_receipt(self, obj):
        return bool(obj.receipt_type)

    def validate_receipt(self, file):
        if file and file.size > MAX_RECEIPT_BYTES:
            raise serializers.ValidationError(f"Receipt must be under {MAX_RECEIPT_BYTES // (1024 * 1024)} MB.")
        return file

    def _store_receipt(self, validated_data):
        file = validated_data.pop("receipt", None)
        if file:
            validated_data.update(
                receipt_data=file.read(),
                receipt_type=getattr(file, "content_type", "") or "image/jpeg",
                receipt_name=(file.name or "receipt")[:200],
            )
        return validated_data

    def create(self, validated_data):
        return super().create(self._store_receipt(validated_data))

    def update(self, instance, validated_data):
        return super().update(instance, self._store_receipt(validated_data))

    def validate(self, attrs):
        plan = self.context["plan"]
        attrs = self._validate_amounts(plan, attrs)
        self._reject_duplicate(plan, attrs)
        return attrs

    def _reject_duplicate(self, plan, attrs):
        attrs["reference"] = normalize_reference(attrs.get("reference", getattr(self.instance, "reference", "")))
        # Fingerprint of the original file (sent by the browser); fall back to hashing the upload itself.
        given = normalize_hash(attrs.get("receipt_hash"))
        receipt = attrs.get("receipt")
        attrs["receipt_hash"] = given or (file_hash(receipt) if receipt else getattr(self.instance, "receipt_hash", ""))

        inst = self.instance
        match, reason = find_duplicate(
            plan.user,
            reference=attrs["reference"],
            receipt_hash=attrs["receipt_hash"],
            plan=plan,
            paid_on=attrs.get("paid_on", getattr(inst, "paid_on", None)),
            paid_time=attrs.get("paid_time", getattr(inst, "paid_time", None)),
            amount=attrs.get("amount", getattr(inst, "amount", None)),
            exclude_pk=getattr(inst, "pk", None),
        )
        if match:
            raise serializers.ValidationError(
                {"duplicate": f"This payment is already recorded ({reason}): {describe(match)}."}
            )

    def _validate_amounts(self, plan, attrs):
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

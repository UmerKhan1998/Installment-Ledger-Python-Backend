from rest_framework import serializers

from .models import CATEGORY_CHOICES, Expense, ExpenseSettings


class ExpenseSettingsSerializer(serializers.ModelSerializer):
    """`monthly_budget` is sent in the tracker currency; it is stored as entered with `budget_currency`."""

    class Meta:
        model = ExpenseSettings
        fields = ("currency", "monthly_budget", "budget_currency")
        read_only_fields = ("budget_currency",)


class ExpenseSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()
    converted = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = (
            "id", "title", "amount", "currency", "category", "category_label", "spent_on", "note",
            "is_recurring", "converted", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")
        # Currency defaults to the user's expense currency (set in the view), so it is optional on create.
        extra_kwargs = {"currency": {"required": False}}

    def get_category_label(self, obj):
        return dict(CATEGORY_CHOICES).get(obj.category, obj.category)

    def get_converted(self, obj):
        """The amount in the tracker currency at the expense month's rate (None when already in it)."""
        conv = self.context.get("converter")
        if conv is None or obj.currency == conv.display:
            return None
        value, row = conv.convert(obj.amount, obj.currency, obj.spent_on.replace(day=1))
        if value is None:
            return {"currency": conv.display, "amount": None, "rate": None, "source": None}
        return {"currency": conv.display, "amount": f"{value:.2f}", "rate": f"{row.rate:.8f}".rstrip("0").rstrip("."), "source": row.source}

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter what the expense was for.")
        return value

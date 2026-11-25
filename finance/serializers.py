from django.utils import timezone
from rest_framework import serializers

from .models import FinancialProfile, Goal


class FinancialProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialProfile
        fields = ("currency", "monthly_income", "other_monthly_income", "cash_savings", "emergency_months_target",
                  "other_debt_payments", "monthly_expenses_override", "investment_horizon_years", "experience",
                  "consent_use_ledger", "consent_ai", "consent_updated_at", "updated_at")
        read_only_fields = ("consent_updated_at", "updated_at")

    def update(self, instance, validated_data):
        if any(k in validated_data and validated_data[k] != getattr(instance, k) for k in ("consent_use_ledger", "consent_ai")):
            validated_data["consent_updated_at"] = timezone.now()
        return super().update(instance, validated_data)


class GoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Goal
        fields = ("id", "name", "kind", "target_amount", "saved_amount", "currency", "target_date", "priority",
                  "assumed_annual_return", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")
        extra_kwargs = {"currency": {"required": False}}

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the goal a name.")
        return value

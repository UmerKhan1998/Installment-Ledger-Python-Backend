"""Financial profile, consent and goals (agents.md §3 Phase 1). Figures here are user-provided unless stated."""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from ledger.models import CURRENCY_CHOICES

MONEY = {"max_digits": 14, "decimal_places": 2, "validators": [MinValueValidator(Decimal("0"))]}


class FinancialProfile(models.Model):
    EXPERIENCE = [("none", "No investing experience"), ("basic", "Some (savings, funds)"), ("experienced", "Experienced")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="financial_profile")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="PKR")
    monthly_income = models.DecimalField(null=True, blank=True, **MONEY)
    other_monthly_income = models.DecimalField(default=Decimal("0"), **MONEY)
    cash_savings = models.DecimalField(null=True, blank=True, **MONEY)
    emergency_months_target = models.PositiveSmallIntegerField(default=6, validators=[MinValueValidator(0), MaxValueValidator(24)])
    other_debt_payments = models.DecimalField(default=Decimal("0"), help_text="Monthly payments on debts not in Plans", **MONEY)
    monthly_expenses_override = models.DecimalField(null=True, blank=True, help_text="Your own estimate; replaces the Expenses average", **MONEY)
    investment_horizon_years = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(60)])
    experience = models.CharField(max_length=20, choices=EXPERIENCE, blank=True)
    # Purpose-specific consent (agents.md §12). Timestamps record when it was last changed.
    consent_use_ledger = models.BooleanField(default=False, help_text="Use my Plans and Expenses for the planner")
    consent_ai = models.BooleanField(default=False, help_text="Send summarised figures (no names) to the AI provider")
    consent_updated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile of {self.user}"


class Goal(models.Model):
    KINDS = [("emergency", "Emergency fund"), ("retirement", "Retirement"), ("education", "Education"), ("home", "Home"),
             ("business", "Business"), ("wealth", "Wealth building"), ("short_term", "Short-term saving"), ("custom", "Other")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="goals")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=KINDS, default="custom")
    target_amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    saved_amount = models.DecimalField(default=Decimal("0"), **MONEY)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="PKR")
    target_date = models.DateField()
    priority = models.PositiveSmallIntegerField(default=2, validators=[MinValueValidator(1), MaxValueValidator(3)])
    # Illustrative only: an assumed yearly return for the scenario column. 0 = no growth assumed.
    assumed_annual_return = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"),
                                                validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("30"))])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "target_date"]

    def __str__(self):
        return self.name

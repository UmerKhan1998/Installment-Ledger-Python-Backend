from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class Plan(models.Model):
    """One thing being paid off in monthly installments (e.g. a MacBook over 18 months)."""

    MONTH_CHOICES = [(12, "12 months"), (18, "18 months"), (24, "24 months")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plans")
    item = models.CharField(max_length=200)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    # What is actually repaid: purchase price + tax + any buffer.
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    months = models.PositiveSmallIntegerField(choices=MONTH_CHOICES, default=18)
    first_payment_date = models.DateField()
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.item} ({self.months} months)"


class Payment(models.Model):
    """A transfer actually sent towards a plan."""

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    paid_on = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-created_at"]

    def __str__(self):
        return f"{self.amount} on {self.paid_on}"

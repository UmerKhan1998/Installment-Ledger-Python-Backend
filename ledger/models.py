from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


CURRENCY_CHOICES = [
    ("USD", "US Dollar"),
    ("PKR", "Pakistani Rupee"),
    ("EUR", "Euro"),
    ("GBP", "British Pound"),
    ("AED", "UAE Dirham"),
    ("SAR", "Saudi Riyal"),
    ("INR", "Indian Rupee"),
    ("CAD", "Canadian Dollar"),
]


class Plan(models.Model):
    """One thing being paid off in monthly installments (e.g. a MacBook over 18 months).

    `currency` is what the debt is in (USD at Best Buy). `payment_currency` is what the
    money is actually sent in (PKR); `exchange_rate` is how many payment-currency units
    buy 1 unit of `currency` (1 USD = 275.77 PKR) and is the default for new payments.
    """

    MONTH_CHOICES = [(12, "12 months"), (18, "18 months"), (24, "24 months")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plans")
    item = models.CharField(max_length=200)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    # What is actually repaid: purchase price + tax + any buffer.
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    months = models.PositiveSmallIntegerField(choices=MONTH_CHOICES, default=18)
    first_payment_date = models.DateField()
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")
    payment_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")
    exchange_rate = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal("1"), validators=[MinValueValidator(Decimal("0.0001"))]
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.item} ({self.months} months)"


class Payment(models.Model):
    """A transfer actually sent towards a plan.

    `amount` is in the plan's currency and is what reduces the balance. When the money was
    sent in another currency, `sent_amount` / `sent_currency` / `exchange_rate` record what
    was actually transferred and the rate used that day (rates move, so each payment keeps its own).
    """

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    sent_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sent_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, blank=True)
    exchange_rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    paid_on = models.DateField()
    paid_time = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    # Receipt image kept in the database (no file storage needed on Render); compressed client-side.
    receipt_data = models.BinaryField(null=True, blank=True, editable=False)
    receipt_type = models.CharField(max_length=50, blank=True)
    receipt_name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-created_at"]

    def __str__(self):
        return f"{self.amount} on {self.paid_on}"

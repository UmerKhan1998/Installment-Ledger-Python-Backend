"""Monthly expense tracking. Independent of installment plans (ledger app)."""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from ledger.models import CURRENCY_CHOICES

CATEGORY_CHOICES = [
    ("food", "Food & groceries"),
    ("rent", "Rent & housing"),
    ("bills", "Bills & utilities"),
    ("transport", "Transport & fuel"),
    ("health", "Health"),
    ("education", "Education"),
    ("shopping", "Shopping"),
    ("family", "Family & gifts"),
    ("entertainment", "Entertainment"),
    ("other", "Other"),
]


class ExpenseSettings(models.Model):
    """Per-user choices for the expense tracker: the currency it is shown in and the linked plans.
    Budgets live in GlobalBudget (versioned) and MonthBudget (one month only)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="expense_settings")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="PKR")
    # Installment plans whose payments also count as monthly expenses (shown read-only, edited in Plans).
    linked_plans = models.ManyToManyField("ledger.Plan", blank=True, related_name="+")

    def __str__(self):
        return f"{self.user} · {self.currency}"


class Expense(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="expenses")
    title = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="PKR")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="other")
    spent_on = models.DateField(db_index=True)
    note = models.CharField(max_length=255, blank=True)
    # Recurring expenses (rent, bills, subscriptions) can be copied into the next month in one click.
    is_recurring = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-spent_on", "-created_at"]
        indexes = [models.Index(fields=["user", "spent_on"])]

    def __str__(self):
        return f"{self.title} {self.currency} {self.amount}"


class MonthlyRate(models.Model):
    """The exchange rate used for one month: 1 `base` = `rate` `quote` (e.g. 1 USD = 276.49740000 PKR).

    Filled from the market the first time a month needs it (past months: the month's last day; the current
    month: the latest rate, refreshed daily) and then kept, so a past month's figures never shift. The user can
    replace it with the rate they actually got (source="manual"), which is never overwritten.
    """

    SOURCE_CHOICES = [("market", "Market"), ("manual", "Manual")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="expense_rates")
    month = models.DateField(help_text="First day of the month")
    base = models.CharField(max_length=3, choices=CURRENCY_CHOICES)
    quote = models.CharField(max_length=3, choices=CURRENCY_CHOICES)
    rate = models.DecimalField(max_digits=24, decimal_places=8, validators=[MinValueValidator(Decimal("0.00000001"))])
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="market")
    as_of = models.DateField(null=True, blank=True, help_text="Market date the rate is from")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "month", "base", "quote"], name="uniq_expense_rate")]
        ordering = ["-month", "base"]

    def __str__(self):
        return f"{self.month:%Y-%m} 1 {self.base} = {self.rate} {self.quote} ({self.source})"


class GlobalBudget(models.Model):
    """One version of the global monthly budget, in force from `effective_from` until the next version.

    Changing the global budget adds a version from the current month, so earlier months keep the budget they had.
    `amount` None means "no budget" from that month on. Amounts are kept exactly as entered, in the currency
    they were entered in; other currencies only convert them for display (no round-trip drift).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="global_budgets")
    effective_from = models.DateField(help_text="First day of the first month this version applies to")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0"))])
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "effective_from"], name="uniq_global_budget_version")]
        ordering = ["-effective_from"]


class MonthBudget(models.Model):
    """A budget for one month only; overrides the global budget for that month until it is reset."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="month_budgets")
    month = models.DateField(help_text="First day of the month")
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "month"], name="uniq_month_budget")]
        ordering = ["-month"]

"""Pure installment math: monthly amount, due dates and how payments cover them."""
import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def add_months(start: date, months: int) -> date:
    """Same day N months later, clamped to the month's last day (Jan 31 -> Feb 28)."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def installment_amounts(total: Decimal, months: int) -> list[Decimal]:
    """Equal monthly amounts; the last one absorbs rounding so the sum is exactly `total`."""
    monthly = (total / months).quantize(CENT, rounding=ROUND_HALF_UP)
    amounts = [monthly] * (months - 1)
    amounts.append(total - monthly * (months - 1))
    return amounts


def monthly_amount(total: Decimal, months: int) -> Decimal:
    return (total / months).quantize(CENT, rounding=ROUND_HALF_UP)


def build_schedule(total: Decimal, months: int, first_date: date, paid_total: Decimal, today: date):
    """Apply everything paid so far to installments in order (oldest first)."""
    rows = []
    left_to_apply = paid_total
    for i, amount in enumerate(installment_amounts(total, months)):
        due = add_months(first_date, i)
        covered = min(amount, max(left_to_apply, Decimal("0")))
        left_to_apply -= covered
        # Money on an installment shows as "partial" even when it is late; lateness is
        # reported separately by `is_overdue` so the overdue count still includes it.
        is_overdue = covered < amount and due < today
        if covered >= amount:
            status = "paid"
        elif covered > 0:
            status = "partial"
        elif is_overdue:
            status = "overdue"
        else:
            status = "upcoming"
        rows.append(
            {
                "number": i + 1,
                "due_date": due,
                "amount": amount,
                "paid_amount": covered,
                "status": status,
                "is_overdue": is_overdue,
            }
        )
    return rows


def to_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def payment_conversion(plan, payments, figures):
    """Figures in the currency the money is sent in (e.g. PKR), at the plan's current rate.

    `sent_total` is what was really transferred: each payment's own sent amount, so a payment
    made at last month's rate keeps last month's rupee figure.
    """
    if plan.payment_currency == plan.currency:
        return None
    rate = plan.exchange_rate
    sent_total = Decimal("0.00")
    for p in payments:
        if p.sent_currency == plan.payment_currency and p.sent_amount is not None:
            sent_total += p.sent_amount
        else:
            sent_total += to_cents(p.amount * rate)
    return {
        "currency": plan.payment_currency,
        "rate": rate,
        "monthly_amount": to_cents(figures["monthly_amount"] * rate),
        "remaining_amount": to_cents(figures["remaining_amount"] * rate),
        "next_due_amount": to_cents(figures["next_due_amount"] * rate),
        "total_amount": to_cents(plan.total_amount * rate),
        "sent_total": sent_total,
    }


def plan_figures(plan, today: date | None = None):
    today = today or date.today()
    payments = list(plan.payments.all())
    paid = sum((p.amount for p in payments), Decimal("0.00"))
    remaining = max(plan.total_amount - paid, Decimal("0.00"))
    schedule = build_schedule(plan.total_amount, plan.months, plan.first_payment_date, paid, today)
    open_rows = [r for r in schedule if r["status"] != "paid"]
    next_due = open_rows[0] if open_rows else None
    figures = {
        "monthly_amount": monthly_amount(plan.total_amount, plan.months),
        "paid_amount": paid,
        "remaining_amount": remaining,
        "installments_paid": len(schedule) - len(open_rows),
        "months_left": len(open_rows),
        "progress_percent": float((paid / plan.total_amount * 100).quantize(Decimal("0.1"))) if plan.total_amount else 0.0,
        "is_complete": remaining == 0,
        "overdue_count": sum(1 for r in schedule if r["is_overdue"]),
        "next_due_date": next_due["due_date"] if next_due else None,
        "next_due_amount": (next_due["amount"] - next_due["paid_amount"]) if next_due else Decimal("0.00"),
        "final_due_date": add_months(plan.first_payment_date, plan.months - 1),
        "schedule": schedule,
    }
    figures["conversion"] = payment_conversion(plan, payments, figures)
    return figures

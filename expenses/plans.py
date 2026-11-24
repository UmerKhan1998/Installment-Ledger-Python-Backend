"""Installment-plan payments shown as monthly expenses (for plans the user linked).

They are derived, never copied: a payment edited or removed in Plans changes Expenses too. The amount used is
what was actually sent (e.g. PKR 20,820 rather than its $75.01 value), so the expense tracker shows real money out.
"""
from ledger.models import Payment
from ledger.schedule import plan_figures

INSTALLMENTS = "installments"
INSTALLMENTS_LABEL = "Installments"


def linked_plan_ids(user, prefs):
    return list(prefs.linked_plans.filter(user=user).values_list("id", flat=True))


def linked_payments(user, plan_ids, start, end):
    """Payments of linked plans in [start, end), as expense-like rows."""
    if not plan_ids:
        return []
    rows = []
    payments = (
        Payment.objects.filter(plan_id__in=plan_ids, plan__user=user, paid_on__gte=start, paid_on__lt=end)
        .select_related("plan").defer("receipt_data").order_by("-paid_on", "-created_at")
    )
    for p in payments:
        sent = p.sent_amount is not None and p.sent_currency
        rows.append({
            "payment_id": p.id,
            "plan_id": p.plan_id,
            "plan_item": p.plan.item,
            "amount": p.sent_amount if sent else p.amount,
            "currency": p.sent_currency if sent else p.plan.currency,
            "spent_on": p.paid_on,
            "paid_time": p.paid_time,
            "note": p.note,
            "has_receipt": bool(p.receipt_type),
        })
    return rows


def due_in_month(plan, start, end):
    """Installments of `plan` due in [start, end): what they ask for and what is still owed on them."""
    figures = plan_figures(plan)
    rows = [r for r in figures["schedule"] if start <= r["due_date"] < end]
    return {
        "numbers": [r["number"] for r in rows],
        "amount": sum((r["amount"] for r in rows), 0),
        "left": sum((r["amount"] - r["paid_amount"] for r in rows), 0),
        "is_complete": figures["is_complete"],
        "monthly_amount": figures["monthly_amount"],
    }

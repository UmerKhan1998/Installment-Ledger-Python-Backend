"""Builds the planner inputs from the profile and (with consent) the existing ledger, then runs the calculator.

Read-only use of ledger/expenses data. Amounts are converted into the profile currency with the same stored
monthly rates as Expenses (expenses.conversion.Converter).
"""
from datetime import date
from decimal import Decimal

from expenses.conversion import Converter
from expenses.models import Expense, ExpenseSettings
from ledger.models import Plan
from ledger.schedule import plan_figures

from .calculations import NEAR_TERM_MONTHS, ZERO, cents, months_between, required_monthly, surplus
from .models import FinancialProfile, Goal

SPENDING_MONTHS = 3


def profile_for(user):
    prefs = ExpenseSettings.objects.filter(user=user).first()
    profile, _ = FinancialProfile.objects.get_or_create(user=user, defaults={"currency": prefs.currency if prefs else "PKR"})
    return profile


def _shift(first, months):
    i = first.year * 12 + first.month - 1 + months
    return date(i // 12, i % 12 + 1, 1)


def ledger_spending(user, currency, today):
    """Average monthly spending over the last SPENDING_MONTHS complete months that have expenses."""
    conv = Converter(user, currency)
    this_month = today.replace(day=1)
    months, unconverted = [], 0
    for back in range(SPENDING_MONTHS, 0, -1):
        first = _shift(this_month, -back)
        rows = Expense.objects.filter(user=user, spent_on__gte=first, spent_on__lt=_shift(first, 1)).values_list("amount", "currency")
        total, count = ZERO, 0
        for amount, cur in rows:
            value, _ = conv.convert(amount, cur, first)
            if value is None:
                unconverted += 1
                continue
            total += value
            count += 1
        if count:
            months.append((first.strftime("%b %Y"), total))
    if not months:
        return {"amount": None, "source": "missing", "detail": f"no expenses recorded in the last {SPENDING_MONTHS} months"}
    avg = cents(sum(t for _, t in months) / len(months))
    detail = f"average of {', '.join(m for m, _ in months)} from your Expenses"
    if len(months) < SPENDING_MONTHS:
        detail += f" (only {len(months)} of {SPENDING_MONTHS} months have data)"
    if unconverted:
        detail += f"; {unconverted} expense(s) skipped for lack of an exchange rate"
    return {"amount": avg, "source": "ledger_expenses", "detail": detail}


def ledger_installments(user, currency, today):
    """Monthly amount of every unfinished plan, converted into the profile currency at this month's rate."""
    conv = Converter(user, currency)
    first = today.replace(day=1)
    total, parts, skipped = ZERO, [], 0
    plans = Plan.objects.filter(user=user).prefetch_related("payments")
    for i, plan in enumerate(plans, start=1):
        figures = plan_figures(plan, today)
        if figures["is_complete"]:
            continue
        value, _ = conv.convert(figures["monthly_amount"], plan.currency, first)
        if value is None:
            skipped += 1
            continue
        total += value
        parts.append(i)
    detail = f"{len(parts)} active plan(s) from Plans" if parts else "no active plans"
    if skipped:
        detail += f"; {skipped} plan(s) skipped for lack of an exchange rate"
    return {"amount": cents(total), "source": "ledger_plans", "detail": detail, "count": len(parts)}


def goal_rows(user, currency, today):
    rows, near_term = [], ZERO
    conv = Converter(user, currency)
    for g in Goal.objects.filter(user=user):
        months = months_between(today, g.target_date)
        need = required_monthly(g.target_amount, g.saved_amount, months)
        scenario = required_monthly(g.target_amount, g.saved_amount, months, g.assumed_annual_return) if g.assumed_annual_return else None
        need_conv, _ = conv.convert(need, g.currency, today.replace(day=1))
        is_near = months <= NEAR_TERM_MONTHS and g.kind != "emergency"
        if is_near and need_conv is not None:
            near_term += need_conv
        rows.append({
            "id": g.id, "name": g.name, "kind": g.kind, "currency": g.currency, "target_amount": f"{g.target_amount:.2f}",
            "saved_amount": f"{g.saved_amount:.2f}", "target_date": g.target_date.isoformat(), "priority": g.priority,
            "months_left": months, "progress_percent": float(min(g.saved_amount / g.target_amount * 100, Decimal(100)).quantize(Decimal("0.1"))),
            "required_monthly": f"{need:.2f}",
            "assumed_annual_return": f"{g.assumed_annual_return:.2f}",
            "scenario_monthly": f"{scenario:.2f}" if scenario is not None else None,
            "near_term": is_near,
            "required_monthly_in_profile_currency": f"{need_conv:.2f}" if need_conv is not None else None,
        })
    return rows, cents(near_term)


def assess(user, today=None):
    today = today or date.today()
    p = profile_for(user)
    cur = p.currency
    income = (p.monthly_income + p.other_monthly_income) if p.monthly_income is not None else None

    if p.monthly_expenses_override is not None:
        spending = {"amount": p.monthly_expenses_override, "source": "user_input", "detail": "your own estimate"}
    elif p.consent_use_ledger:
        spending = ledger_spending(user, cur, today)
    else:
        spending = {"amount": None, "source": "missing", "detail": "allow use of your ledger or enter an estimate"}

    installments = ledger_installments(user, cur, today) if p.consent_use_ledger else {
        "amount": ZERO, "source": "missing", "detail": "allow use of your ledger to include your plans", "count": 0}

    goals, near_term = goal_rows(user, cur, today)
    result = surplus(cur, income, spending, installments, p.other_debt_payments, p.cash_savings,
                     p.emergency_months_target, near_term).as_dict()
    if not p.consent_use_ledger:
        result["notes"].append("Your Plans and Expenses aren't used until you allow it, so installments count as 0.")
    result["goals"] = goals
    result["as_of"] = today.isoformat()
    result["profile_updated_at"] = p.updated_at.isoformat() if p.updated_at else None
    return result

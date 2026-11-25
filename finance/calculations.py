"""Deterministic planning maths (agents.md §7). Decimal for money; every result lists its inputs and steps.

METHOD_VERSION identifies the rules below; bump it when they change so stored/AI-explained results stay traceable.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

METHOD_VERSION = "surplus-v1"
CENT = Decimal("0.01")
ZERO = Decimal("0")

# Configurable assumptions (documented on the page).
BUFFER_SHARE = Decimal("0.10")        # keep 10% of free cash flow unallocated for irregular costs
EMERGENCY_BUILD_MONTHS = 12           # close an emergency-fund gap over a year
NEAR_TERM_MONTHS = 36                 # goals due within 3 years are funded from savings, not investments


def cents(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def months_between(start: date, end: date) -> int:
    """Whole months from `start` to `end` (0 if end is not after start)."""
    months = (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)
    return max(months, 0)


def required_monthly(target, saved, months, annual_return_pct=ZERO):
    """Monthly contribution so that `saved` plus contributions reach `target` in `months`.

    0% return: (target - saved) / months. With r = annual/12/100 (end-of-month contributions):
    PMT = (FV - PV(1+r)^n) * r / ((1+r)^n - 1). Illustrative only for r > 0.
    """
    target, saved = Decimal(target), Decimal(saved)
    if target <= saved:
        return ZERO
    if months <= 0:
        return cents(target - saved)
    r = Decimal(annual_return_pct) / Decimal(1200)
    if r == 0:
        return cents((target - saved) / months)
    growth = (1 + r) ** months
    needed = (target - saved * growth) * r / (growth - 1)
    return cents(max(needed, ZERO))


@dataclass
class Line:
    id: str
    label: str
    amount: Decimal | None
    source: str          # user_input | ledger_expenses | ledger_plans | calculation | missing
    detail: str = ""
    sign: str = ""       # "+", "-", "=" for the waterfall

    def as_dict(self):
        return {"id": self.id, "label": self.label, "amount": f"{self.amount:.2f}" if self.amount is not None else None,
                "source": self.source, "detail": self.detail, "sign": self.sign}


@dataclass
class SurplusResult:
    currency: str
    lines: list = field(default_factory=list)
    investable: Decimal = ZERO
    free_cash_flow: Decimal | None = None
    emergency_target: Decimal | None = None
    emergency_gap: Decimal | None = None
    missing: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    complete: bool = True

    def as_dict(self):
        fmt = lambda v: f"{v:.2f}" if v is not None else None  # noqa: E731
        return {
            "method_version": METHOD_VERSION, "currency": self.currency, "lines": [l.as_dict() for l in self.lines],
            "investable_monthly": fmt(self.investable), "free_cash_flow": fmt(self.free_cash_flow),
            "emergency_target": fmt(self.emergency_target), "emergency_gap": fmt(self.emergency_gap),
            "missing": self.missing, "notes": self.notes, "complete": self.complete,
            "assumptions": {"buffer_share": str(BUFFER_SHARE), "emergency_build_months": EMERGENCY_BUILD_MONTHS,
                            "near_term_months": NEAR_TERM_MONTHS},
        }


def surplus(currency, income, expenses, installments, other_debts, savings, emergency_months, near_term_goal_contributions):
    """Conservative monthly investable surplus. Inputs: Decimals or None (unknown). Pure function."""
    res = SurplusResult(currency=currency)
    add = res.lines.append

    add(Line("income", "Monthly income", income, "user_input" if income is not None else "missing", sign="+"))
    add(Line("expenses", "Monthly spending", expenses["amount"], expenses["source"], expenses.get("detail", ""), "-"))
    add(Line("installments", "Installment plans", installments["amount"], installments["source"], installments.get("detail", ""), "-"))
    add(Line("other_debts", "Other debt payments", other_debts, "user_input", sign="-"))

    if income is None:
        res.missing.append("Monthly income")
    if expenses["amount"] is None:
        res.missing.append("Monthly spending (add expenses, allow use of your ledger, or enter an estimate)")
    if savings is None:
        res.missing.append("Cash savings")
    if res.missing and (income is None or expenses["amount"] is None):
        res.complete = False
        res.notes.append("Not enough information to estimate an investable amount yet.")
        return res

    outgoings = expenses["amount"] + (installments["amount"] or ZERO) + other_debts
    free = cents(income - outgoings)
    res.free_cash_flow = free
    add(Line("free_cash_flow", "Free cash flow", free, "calculation", "income − spending − installments − other debts", "="))

    res.emergency_target = cents(outgoings * emergency_months)
    res.emergency_gap = cents(max(res.emergency_target - (savings or ZERO), ZERO)) if savings is not None else res.emergency_target
    if free <= 0:
        res.notes.append("Your monthly outgoings use up your income, so nothing is investable right now.")
        add(Line("investable", "Estimated investable per month", ZERO, "calculation", "", "="))
        return res

    buffer = cents(free * BUFFER_SHARE)
    add(Line("buffer", "Safety buffer", buffer, "calculation", f"{BUFFER_SHARE * 100:.0f}% of free cash flow kept for irregular costs", "-"))
    left = free - buffer

    emergency = cents(min(res.emergency_gap / EMERGENCY_BUILD_MONTHS, left)) if res.emergency_gap else ZERO
    add(Line("emergency", "Emergency fund top-up", emergency, "calculation",
             f"gap of {res.emergency_gap:.2f} ({emergency_months} months of outgoings minus savings) closed over {EMERGENCY_BUILD_MONTHS} months"
             if res.emergency_gap else "emergency fund already covers your target", "-"))
    left -= emergency

    near = cents(min(near_term_goal_contributions, left))
    add(Line("near_term_goals", "Near-term goals", near, "calculation",
             f"monthly amounts needed for goals due within {NEAR_TERM_MONTHS} months (kept in savings, not invested)", "-"))
    left -= near

    res.investable = cents(max(left, ZERO))
    add(Line("investable", "Estimated investable per month", res.investable, "calculation", "what is left after the steps above", "="))
    if savings is None:
        res.complete = False
        res.notes.append("Savings unknown: the emergency top-up assumes you have none, so this is very cautious.")
    return res

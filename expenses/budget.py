"""Which budget applies to a month: its own (MonthBudget), else the global version in force, else none."""
from .models import GlobalBudget, MonthBudget

FAR_PAST = "2000-01-01"


def global_version(user, first):
    """The GlobalBudget version in force for month `first` (latest effective_from <= first), or None."""
    return GlobalBudget.objects.filter(user=user, effective_from__lte=first).order_by("-effective_from").first()


def resolve(user, first):
    """{"amount", "currency", "source": "month" | "global", "global": version or None, "override": MonthBudget or None}.

    amount/currency are None when no budget applies.
    """
    override = MonthBudget.objects.filter(user=user, month=first).first()
    version = global_version(user, first)
    if override:
        return {"amount": override.amount, "currency": override.currency, "source": "month", "global": version, "override": override}
    if version and version.amount is not None:
        return {"amount": version.amount, "currency": version.currency, "source": "global", "global": version, "override": None}
    return {"amount": None, "currency": None, "source": None, "global": version, "override": None}


def version_payload(v):
    if not v:
        return None
    return {
        "amount": f"{v.amount:.2f}" if v.amount is not None else None,
        "currency": v.currency,
        "effective_from": v.effective_from.strftime("%Y-%m"),
        "since_start": v.effective_from.isoformat() == FAR_PAST,
    }

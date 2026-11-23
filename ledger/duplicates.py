"""Finds an existing payment that a new one would duplicate (same receipt recorded twice)."""
import hashlib
import re

from .models import Payment

HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_reference(value):
    return re.sub(r"\s+", "", value or "").upper()[:64]


def normalize_hash(value):
    value = (value or "").strip().lower()
    return value if HASH_RE.match(value) else ""


def file_hash(file):
    digest = hashlib.sha256()
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def find_duplicate(user, *, reference="", receipt_hash="", plan=None, paid_on=None, paid_time=None, amount=None, exclude_pk=None):
    """Return (payment, reason) for the first match, else (None, None).

    1. Same bank reference (STAN / TID / Ref) anywhere in the user's ledger.
    2. Same receipt file (SHA-256 of the original image).
    3. Same plan + date + time + amount (manual entries / receipts without a reference).
    """
    payments = Payment.objects.filter(plan__user=user).select_related("plan").defer("receipt_data")
    if exclude_pk:
        payments = payments.exclude(pk=exclude_pk)

    if reference:
        match = payments.filter(reference=reference).first()
        if match:
            return match, f"reference {reference}"
    if receipt_hash:
        match = payments.filter(receipt_hash=receipt_hash).first()
        if match:
            return match, "the same receipt image"
    if plan is not None and paid_on and paid_time and amount is not None:
        match = payments.filter(plan=plan, paid_on=paid_on, paid_time=paid_time, amount=amount).first()
        if match:
            return match, "the same date, time and amount"
    return None, None


def describe(payment):
    when = payment.paid_on.strftime("%b %d, %Y").replace(" 0", " ")
    if payment.paid_time:
        when += f" {payment.paid_time:%H:%M}"
    sent = f" ({payment.sent_currency} {payment.sent_amount:,.2f})" if payment.sent_amount else ""
    return f'{payment.plan.currency} {payment.amount:,.2f}{sent} on {when} in "{payment.plan.item}"'

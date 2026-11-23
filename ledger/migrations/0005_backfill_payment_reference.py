import re

from django.db import migrations

REF = re.compile(r"\bRef\s+([A-Za-z0-9-]{4,})", re.IGNORECASE)


def backfill(apps, schema_editor):
    """Payments saved before this change kept the receipt reference only in the note ("Raast · Ref 100441")."""
    Payment = apps.get_model("ledger", "Payment")
    for payment in Payment.objects.filter(reference="").exclude(note=""):
        match = REF.search(payment.note)
        if match:
            payment.reference = match.group(1).upper()
            payment.save(update_fields=["reference"])


class Migration(migrations.Migration):
    dependencies = [("ledger", "0004_payment_duplicate_keys")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]

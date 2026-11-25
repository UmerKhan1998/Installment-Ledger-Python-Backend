"""python manage.py move_receipts_to_firebase [--dry-run]

Copies receipts kept in the database into Firebase Storage, then clears them from the database.
Each one is checked (downloaded back and compared) before the database copy is removed.
"""
from django.core.management.base import BaseCommand, CommandError

from ledger import receipt_storage
from ledger.models import Payment


class Command(BaseCommand):
    help = "Move receipt images from the database to Firebase Storage."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count what would move.")

    def handle(self, *args, dry_run=False, **options):
        if not receipt_storage.enabled():
            raise CommandError("Firebase Storage isn't configured: set FIREBASE_STORAGE_BUCKET and a service account.")
        pending = Payment.objects.filter(receipt_path="").exclude(receipt_data=None).select_related("plan")
        self.stdout.write(f"{pending.count()} receipt(s) in the database.")
        if dry_run:
            return
        moved = failed = 0
        for payment in pending.iterator():
            data = bytes(payment.receipt_data)
            try:
                path = receipt_storage.save(payment.plan.user_id, payment.pk, data, payment.receipt_type)
                if receipt_storage.load(path) != data:
                    raise RuntimeError("verification mismatch")
            except Exception as exc:
                failed += 1
                self.stderr.write(f"payment {payment.pk}: {exc}")
                continue
            Payment.objects.filter(pk=payment.pk).update(receipt_path=path, receipt_data=None)
            moved += 1
        self.stdout.write(self.style.SUCCESS(f"Moved {moved}, failed {failed}."))

"""Deleting a payment (directly, in bulk, or with its plan) also deletes its receipt object in Firebase Storage."""
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from . import receipt_storage
from .models import Payment


@receiver(post_delete, sender=Payment)
def delete_receipt_object(sender, instance, **kwargs):
    if instance.receipt_path:
        path = instance.receipt_path
        transaction.on_commit(lambda: receipt_storage.delete(path))

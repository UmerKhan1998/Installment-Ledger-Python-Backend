"""Where receipt images live: Firebase Storage (Google Cloud Storage) when configured, else the database.

Firebase Storage needs a service account (FIREBASE_SERVICE_ACCOUNT_FILE or FIREBASE_SERVICE_ACCOUNT_B64) and
FIREBASE_STORAGE_BUCKET. Uploads go through Django (which validates them first), the bucket is not exposed to
browsers, and receipts are served back through the API, so web and mobile don't change.
Objects: users/<user id>/receipts/<payment id>-<random>.jpg
"""
import base64
import json
import logging
import uuid

from django.conf import settings

log = logging.getLogger(__name__)
_bucket = None


def _credentials_info():
    if settings.FIREBASE_SERVICE_ACCOUNT_B64:
        return json.loads(base64.b64decode(settings.FIREBASE_SERVICE_ACCOUNT_B64))
    if settings.FIREBASE_SERVICE_ACCOUNT_FILE:
        with open(settings.FIREBASE_SERVICE_ACCOUNT_FILE) as fh:
            return json.load(fh)
    return None


def enabled():
    return bool(settings.FIREBASE_STORAGE_BUCKET and (settings.FIREBASE_SERVICE_ACCOUNT_B64 or settings.FIREBASE_SERVICE_ACCOUNT_FILE))


def bucket():
    """The Firebase Storage bucket (firebase_admin app initialised once, lazily)."""
    global _bucket
    if _bucket is None:
        import firebase_admin
        from firebase_admin import credentials, storage

        info = _credentials_info()
        if not info:
            raise RuntimeError("Firebase Storage is not configured (no service account).")
        try:
            app = firebase_admin.get_app("receipts")
        except ValueError:
            app = firebase_admin.initialize_app(
                credentials.Certificate(info), {"storageBucket": settings.FIREBASE_STORAGE_BUCKET}, name="receipts",
            )
        _bucket = storage.bucket(app=app)
    return _bucket


def object_path(user_id, payment_id):
    return f"users/{user_id}/receipts/{payment_id}-{uuid.uuid4().hex[:12]}.jpg"


def save(user_id, payment_id, data, content_type):
    path = object_path(user_id, payment_id)
    blob = bucket().blob(path)
    blob.cache_control = "private, max-age=3600"
    blob.upload_from_string(data, content_type=content_type or "image/jpeg")
    return path


def load(path):
    return bucket().blob(path).download_as_bytes()


def delete(path):
    try:
        bucket().blob(path).delete()
    except Exception:  # already gone, or storage unavailable: never block deleting the payment
        log.warning("Could not delete receipt object %s", path, exc_info=True)

from django.conf import settings
from django.db import models


class FirebaseIdentity(models.Model):
    """Links a Firebase Auth user (uid) to a Django user. Data (plans, expenses...) stays on the Django user."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="firebase")
    uid = models.CharField(max_length=128, unique=True)
    email = models.EmailField(blank=True)
    sign_in_provider = models.CharField(max_length=40, blank=True)  # password, google.com, ...
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.email or self.uid} -> {self.user_id}"

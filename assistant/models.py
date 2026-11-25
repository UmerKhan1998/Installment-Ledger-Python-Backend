from django.conf import settings
from django.db import models


class AISettings(models.Model):
    """A user's AI provider/model choice and the last connection test. The API key is never stored here:
    it is a server secret (settings.GEMINI_API_KEY)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_settings")
    provider = models.CharField(max_length=30, default="gemini")
    model = models.CharField(max_length=100, blank=True, help_text="Empty = the server default (GEMINI_MODEL)")
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True)
    last_test_model_version = models.CharField(max_length=100, blank=True)
    last_test_message = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} · {self.provider}/{self.model or 'default'}"

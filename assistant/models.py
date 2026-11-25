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


class AIInteraction(models.Model):
    """Audit record of one AI call (agents.md §21): what was asked for, what answered, what was checked.
    Stores the validated answer and the IDs of the figures sent — never the API key or raw ledger rows."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_interactions")
    kind = models.CharField(max_length=30)  # planner
    prompt_version = models.CharField(max_length=40)
    method_version = models.CharField(max_length=40, blank=True)
    model_requested = models.CharField(max_length=100)
    model_version = models.CharField(max_length=100, blank=True)
    used_fallback = models.BooleanField(default=False)
    ok = models.BooleanField(default=False)
    error_kind = models.CharField(max_length=30, blank=True)
    latency_ms = models.PositiveIntegerField(null=True)
    input_tokens = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    figure_ids = models.JSONField(default=list)
    figures_hash = models.CharField(max_length=64, blank=True)
    answer = models.JSONField(null=True, blank=True)
    warnings = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

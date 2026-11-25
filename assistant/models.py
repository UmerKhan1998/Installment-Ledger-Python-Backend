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


class PlannerChat(models.Model):
    """One conversation with the AI planner (the panel's "Old chats"). Owned by one user; never shared."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="planner_chats")
    title = models.CharField(max_length=120)
    page = models.CharField(max_length=40, blank=True, help_text="Screen the chat was started from, e.g. Invest")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.user} · {self.title}"


class PlannerMessage(models.Model):
    """A turn in a planner chat. `out` is the validated planner response (same shape the API returned), so an old
    chat re-renders exactly as it was answered, with the figures as of that day."""

    ROLES = (("user", "User"), ("ai", "AI"), ("error", "Error"))
    chat = models.ForeignKey(PlannerChat, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ROLES)
    text = models.TextField(blank=True)  # the question, or the error shown
    question = models.CharField(max_length=500, blank=True)  # for ai/error: the question it answers (retry)
    out = models.JSONField(null=True, blank=True)
    error_kind = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

from django.conf import settings
from django.db import models


class Note(models.Model):
    """A free-form note. Owned by one user; never shared. Pinned notes show first, then the most recently edited."""

    COLORS = (("", "Default"), ("yellow", "Yellow"), ("green", "Green"), ("blue", "Blue"), ("pink", "Pink"), ("purple", "Purple"))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notes")
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    color = models.CharField(max_length=10, choices=COLORS, blank=True, default="")
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-pinned", "-updated_at", "-id"]
        indexes = [models.Index(fields=["user", "-pinned", "-updated_at"])]

    def __str__(self):
        return f"{self.user} · {self.title or self.body[:40]}"

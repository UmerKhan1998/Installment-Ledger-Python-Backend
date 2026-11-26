from django.conf import settings
from django.db import models


class Task(models.Model):
    """One to-do item. The same rows feed the list view and the Kanban board: `status` is the board column and
    `position` the order inside it (0 = top). Owned by one user; never shared."""

    TODO, DOING, DONE = "todo", "doing", "done"
    STATUSES = ((TODO, "To do"), (DOING, "In progress"), (DONE, "Done"))
    PRIORITIES = ((1, "High"), (2, "Medium"), (3, "Low"))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=TODO)
    priority = models.PositiveSmallIntegerField(choices=PRIORITIES, default=2)
    due_date = models.DateField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["user", "status", "position"])]

    def __str__(self):
        return f"{self.user} · {self.title}"

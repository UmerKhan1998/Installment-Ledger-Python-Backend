"""Ordering rules for tasks: a column (status) is a dense list 0..n-1 per user."""
from django.db import transaction
from django.utils import timezone

from .models import Task

MAX_TASKS = 1000


def column(user, status):
    return list(Task.objects.filter(user=user, status=status).order_by("position", "id"))


def renumber(tasks):
    changed = []
    for i, t in enumerate(tasks):
        if t.position != i:
            t.position = i
            changed.append(t)
    if changed:
        Task.objects.bulk_update(changed, ["position"])


def stamp(task, status):
    """Set the status and keep `completed_at` in step with it."""
    was_done = task.status == Task.DONE and task.completed_at is not None
    task.status = status
    if status == Task.DONE:
        if not was_done:
            task.completed_at = timezone.now()
    else:
        task.completed_at = None


@transaction.atomic
def place(task, status, index=None):
    """Put `task` into column `status` at `index` (None or past the end = bottom) and renumber both columns."""
    old_status = task.status
    target = [t for t in column(task.user, status) if t.pk != task.pk]
    index = len(target) if index is None else max(0, min(int(index), len(target)))
    stamp(task, status)
    task.position = index
    task.save()
    target.insert(index, task)
    renumber(target)
    if old_status != status:
        renumber(column(task.user, old_status))
    return task

"""To-do tasks for the signed-in user; one set of rows serves the list view and the Kanban board."""
from django.db import transaction
from django.db.models import Q
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Task
from .serializers import TaskSerializer
from .services import MAX_TASKS, column, place, renumber

STATUSES = {s for s, _ in Task.STATUSES}


class TaskListCreateView(generics.ListCreateAPIView):
    """GET /api/tasks/?status=todo|doing|done&q=text -> tasks ordered by column position.
    POST {title, notes?, status?, priority?, due_date?} -> new task at the bottom of its column."""

    serializer_class = TaskSerializer
    pagination_class = None

    def get_queryset(self):
        qs = Task.objects.filter(user=self.request.user)
        status = self.request.query_params.get("status")
        if status in STATUSES:
            qs = qs.filter(status=status)
        q = " ".join(self.request.query_params.get("q", "").split())[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(notes__icontains=q))
        return qs.order_by("status", "position", "id")

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        if Task.objects.filter(user=user).count() >= MAX_TASKS:
            raise ValidationError({"detail": f"You can keep up to {MAX_TASKS} tasks. Clear some finished ones first."})
        task = serializer.save(user=user)
        place(task, task.status)


class TaskDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET / PATCH / DELETE /api/tasks/{id}/. Changing `status` here moves the task to the bottom of that column."""

    serializer_class = TaskSerializer

    def get_queryset(self):
        return Task.objects.filter(user=self.request.user)

    @transaction.atomic
    def perform_update(self, serializer):
        old_status = serializer.instance.status
        new_status = serializer.validated_data.get("status", old_status)
        serializer.validated_data.pop("status", None)
        task = serializer.save()
        if new_status != old_status:
            task.status = old_status  # `place` records the move from the old column
            place(task, new_status)

    @transaction.atomic
    def perform_destroy(self, instance):
        user, status = instance.user, instance.status
        instance.delete()
        renumber(column(user, status))


class TaskMoveView(APIView):
    """POST /api/tasks/{id}/move/ {"status": "doing", "index": 0} -> drop a card at a place in a column
    (index omitted = bottom). Returns every task so the board can redraw from the server's order."""

    def post(self, request, pk):
        task = Task.objects.filter(pk=pk, user=request.user).first()
        if not task:
            return Response({"detail": "Task not found."}, status=404)
        status = request.data.get("status", task.status)
        if status not in STATUSES:
            return Response({"status": ["Must be todo, doing or done."]}, status=400)
        index = request.data.get("index")
        if index is not None:
            try:
                index = int(index)
            except (TypeError, ValueError):
                return Response({"index": ["Must be a whole number."]}, status=400)
        place(task, status, index)
        tasks = Task.objects.filter(user=request.user).order_by("status", "position", "id")
        return Response(TaskSerializer(tasks, many=True).data)


class TaskClearDoneView(APIView):
    """POST /api/tasks/clear-done/ -> delete the user's finished tasks. Returns how many were removed."""

    def post(self, request):
        deleted, _ = Task.objects.filter(user=request.user, status=Task.DONE).delete()
        return Response({"deleted": deleted})

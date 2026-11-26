from django.urls import path

from .views import TaskClearDoneView, TaskDetailView, TaskListCreateView, TaskMoveView

urlpatterns = [
    path("", TaskListCreateView.as_view(), name="task-list"),
    path("clear-done/", TaskClearDoneView.as_view(), name="task-clear-done"),
    path("<int:pk>/", TaskDetailView.as_view(), name="task-detail"),
    path("<int:pk>/move/", TaskMoveView.as_view(), name="task-move"),
]

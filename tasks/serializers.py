from rest_framework import serializers

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = ("id", "title", "notes", "status", "priority", "due_date", "position", "completed_at", "created_at", "updated_at")
        read_only_fields = ("id", "position", "completed_at", "created_at", "updated_at")

    def validate_title(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Give the task a title.")
        return value

from rest_framework import serializers

from .models import Note

MAX_BODY = 20_000


class NoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Note
        fields = ("id", "title", "body", "color", "pinned", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_title(self, value):
        return " ".join(value.split())

    def validate_body(self, value):
        value = value.replace("\r\n", "\n").strip()
        if len(value) > MAX_BODY:
            raise serializers.ValidationError(f"Keep a note under {MAX_BODY:,} characters.")
        return value

    def validate(self, attrs):
        title = attrs.get("title", self.instance.title if self.instance else "")
        body = attrs.get("body", self.instance.body if self.instance else "")
        if not title and not body:
            raise serializers.ValidationError({"body": ["Write something first: a title or some text."]})
        return attrs

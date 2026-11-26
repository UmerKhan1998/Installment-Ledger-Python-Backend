"""Notes for the signed-in user."""
from django.db.models import Q
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from .models import Note
from .serializers import NoteSerializer

MAX_NOTES = 2000


class NoteListCreateView(generics.ListCreateAPIView):
    """GET /api/notes/?q=text -> notes, pinned first then most recently edited.
    POST {title?, body?, color?, pinned?} (a title or some text is required)."""

    serializer_class = NoteSerializer
    pagination_class = None

    def get_queryset(self):
        qs = Note.objects.filter(user=self.request.user)
        q = " ".join(self.request.query_params.get("q", "").split())[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(body__icontains=q))
        return qs

    def perform_create(self, serializer):
        if Note.objects.filter(user=self.request.user).count() >= MAX_NOTES:
            raise ValidationError({"detail": f"You can keep up to {MAX_NOTES} notes. Delete some old ones first."})
        serializer.save(user=self.request.user)


class NoteDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET / PATCH / DELETE /api/notes/{id}/"""

    serializer_class = NoteSerializer

    def get_queryset(self):
        return Note.objects.filter(user=self.request.user)

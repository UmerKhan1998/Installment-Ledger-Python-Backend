from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .assessment import assess, profile_for
from .models import Goal
from .serializers import FinancialProfileSerializer, GoalSerializer


class ProfileView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/finance/profile/"""

    serializer_class = FinancialProfileSerializer

    def get_object(self):
        return profile_for(self.request.user)


class AssessmentView(APIView):
    """GET /api/finance/assessment/ -> cash flow, emergency fund, investable surplus (with steps), goals."""

    def get(self, request):
        return Response(assess(request.user))


class GoalListCreateView(generics.ListCreateAPIView):
    serializer_class = GoalSerializer
    pagination_class = None

    def get_queryset(self):
        return Goal.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        currency = serializer.validated_data.get("currency") or profile_for(self.request.user).currency
        serializer.save(user=self.request.user, currency=currency)


class GoalDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = GoalSerializer

    def get_queryset(self):
        return Goal.objects.filter(user=self.request.user)

from datetime import date
from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Payment, Plan
from .schedule import plan_figures
from .serializers import PaymentSerializer, PlanSerializer


def user_plans(request):
    return Plan.objects.filter(user=request.user).prefetch_related("payments")


class PlanListCreateView(generics.ListCreateAPIView):
    serializer_class = PlanSerializer
    pagination_class = None

    def get_queryset(self):
        return user_plans(self.request)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "with_detail": self.request.method == "POST"}


class PlanDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PlanSerializer

    def get_queryset(self):
        return user_plans(self.request)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "with_detail": True}


class PlanPaymentMixin:
    serializer_class = PaymentSerializer
    pagination_class = None

    def get_plan(self):
        if not hasattr(self, "_plan"):
            self._plan = get_object_or_404(user_plans(self.request), pk=self.kwargs["plan_id"])
        return self._plan

    def get_queryset(self):
        return Payment.objects.filter(plan=self.get_plan())

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "plan": self.get_plan()}


class PaymentListCreateView(PlanPaymentMixin, generics.ListCreateAPIView):
    def perform_create(self, serializer):
        serializer.save(plan=self.get_plan())


class PaymentDetailView(PlanPaymentMixin, generics.RetrieveUpdateDestroyAPIView):
    pass


class SummaryView(APIView):
    """Totals across all of the user's plans, for the dashboard header."""

    def get(self, request):
        plans = list(user_plans(request))
        today = date.today()
        figures = [plan_figures(p, today) for p in plans]
        active = [f for f in figures if not f["is_complete"]]
        upcoming = [f["next_due_date"] for f in active if f["next_due_date"]]
        return Response(
            {
                "plan_count": len(plans),
                "active_plan_count": len(active),
                "total_amount": f"{sum((p.total_amount for p in plans), Decimal('0')):.2f}",
                "total_paid": f"{sum((f['paid_amount'] for f in figures), Decimal('0')):.2f}",
                "total_left": f"{sum((f['remaining_amount'] for f in figures), Decimal('0')):.2f}",
                "monthly_commitment": f"{sum((f['monthly_amount'] for f in active), Decimal('0')):.2f}",
                "overdue_count": sum(f["overdue_count"] for f in figures),
                "next_due_date": min(upcoming).isoformat() if upcoming else None,
            }
        )

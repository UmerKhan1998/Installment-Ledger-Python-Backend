from django.urls import path

from .views import (
    LiveRateView,
    PaymentDetailView,
    PaymentListCreateView,
    PlanDetailView,
    PlanListCreateView,
    SummaryView,
)

urlpatterns = [
    path("summary/", SummaryView.as_view(), name="summary"),
    path("rates/", LiveRateView.as_view(), name="rates"),
    path("plans/", PlanListCreateView.as_view(), name="plan-list"),
    path("plans/<int:pk>/", PlanDetailView.as_view(), name="plan-detail"),
    path("plans/<int:plan_id>/payments/", PaymentListCreateView.as_view(), name="payment-list"),
    path("plans/<int:plan_id>/payments/<int:pk>/", PaymentDetailView.as_view(), name="payment-detail"),
]

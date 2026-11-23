from django.urls import path

from .views import (
    DuplicateCheckView,
    LiveRateView,
    PaymentBulkDeleteView,
    PaymentDetailView,
    PaymentListCreateView,
    PlanDetailView,
    PlanListCreateView,
    ReceiptView,
    SummaryView,
)

urlpatterns = [
    path("summary/", SummaryView.as_view(), name="summary"),
    path("rates/", LiveRateView.as_view(), name="rates"),
    path("payments/duplicate-check/", DuplicateCheckView.as_view(), name="duplicate-check"),
    path("plans/", PlanListCreateView.as_view(), name="plan-list"),
    path("plans/<int:pk>/", PlanDetailView.as_view(), name="plan-detail"),
    path("plans/<int:plan_id>/payments/", PaymentListCreateView.as_view(), name="payment-list"),
    path("plans/<int:plan_id>/payments/bulk-delete/", PaymentBulkDeleteView.as_view(), name="payment-bulk-delete"),
    path("plans/<int:plan_id>/payments/<int:pk>/", PaymentDetailView.as_view(), name="payment-detail"),
    path("plans/<int:plan_id>/payments/<int:pk>/receipt/", ReceiptView.as_view(), name="payment-receipt"),
]

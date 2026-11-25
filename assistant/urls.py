from django.urls import path

from .views import AIModelsView, AISettingsView, AITestView, PlannerView

urlpatterns = [
    path("settings/", AISettingsView.as_view(), name="ai-settings"),
    path("models/", AIModelsView.as_view(), name="ai-models"),
    path("test/", AITestView.as_view(), name="ai-test"),
    path("planner/", PlannerView.as_view(), name="ai-planner"),
]

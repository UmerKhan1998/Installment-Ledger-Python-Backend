from django.urls import path

from .views import AssessmentView, GoalDetailView, GoalListCreateView, ProfileView

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="finance-profile"),
    path("assessment/", AssessmentView.as_view(), name="finance-assessment"),
    path("goals/", GoalListCreateView.as_view(), name="goal-list"),
    path("goals/<int:pk>/", GoalDetailView.as_view(), name="goal-detail"),
]

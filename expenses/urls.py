from django.urls import path

from .projects import (
    ProjectDetailView,
    ProjectExpenseBulkDeleteView,
    ProjectExpenseDetailView,
    ProjectExpenseListCreateView,
    ProjectListCreateView,
    ProjectTemplatesView,
)

from .views import (
    BudgetView,
    CategoriesView,
    CopyRecurringView,
    ExpenseBulkDeleteView,
    ExpenseDetailView,
    ExpenseListCreateView,
    ExpenseSettingsView,
    ExpenseSummaryView,
    MonthRatesView,
)

urlpatterns = [
    path("", ExpenseListCreateView.as_view(), name="expense-list"),
    path("<int:pk>/", ExpenseDetailView.as_view(), name="expense-detail"),
    path("summary/", ExpenseSummaryView.as_view(), name="expense-summary"),
    path("settings/", ExpenseSettingsView.as_view(), name="expense-settings"),
    path("rates/", MonthRatesView.as_view(), name="expense-rates"),
    path("budget/", BudgetView.as_view(), name="expense-budget"),
    path("categories/", CategoriesView.as_view(), name="expense-categories"),
    path("bulk-delete/", ExpenseBulkDeleteView.as_view(), name="expense-bulk-delete"),
    path("copy-recurring/", CopyRecurringView.as_view(), name="expense-copy-recurring"),
    path("projects/", ProjectListCreateView.as_view(), name="project-list"),
    path("projects/templates/", ProjectTemplatesView.as_view(), name="project-templates"),
    path("projects/<int:pk>/", ProjectDetailView.as_view(), name="project-detail"),
    path("projects/<int:project_id>/expenses/", ProjectExpenseListCreateView.as_view(), name="project-expense-list"),
    path("projects/<int:project_id>/expenses/bulk-delete/", ProjectExpenseBulkDeleteView.as_view(), name="project-expense-bulk-delete"),
    path("projects/<int:project_id>/expenses/<int:pk>/", ProjectExpenseDetailView.as_view(), name="project-expense-detail"),
]

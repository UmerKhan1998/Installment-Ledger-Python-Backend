from django.urls import path

from .projects import (
    ProjectDetailView,
    ProjectExpenseBulkDeleteView,
    ProjectExpenseDetailView,
    ProjectExpenseListCreateView,
    ProjectListCreateView,
    ProjectTemplatesView,
)

from .transfer_views import ExpenseExportView, ImportCommitView, ImportPreviewView, ProjectExportView
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
    RateHistoryView,
    RateRefreshView,
)

urlpatterns = [
    path("", ExpenseListCreateView.as_view(), name="expense-list"),
    path("<int:pk>/", ExpenseDetailView.as_view(), name="expense-detail"),
    path("summary/", ExpenseSummaryView.as_view(), name="expense-summary"),
    path("settings/", ExpenseSettingsView.as_view(), name="expense-settings"),
    path("rates/", MonthRatesView.as_view(), name="expense-rates"),
    path("rates/history/", RateHistoryView.as_view(), name="expense-rate-history"),
    path("rates/refresh/", RateRefreshView.as_view(), name="expense-rate-refresh"),
    path("budget/", BudgetView.as_view(), name="expense-budget"),
    path("categories/", CategoriesView.as_view(), name="expense-categories"),
    path("bulk-delete/", ExpenseBulkDeleteView.as_view(), name="expense-bulk-delete"),
    path("copy-recurring/", CopyRecurringView.as_view(), name="expense-copy-recurring"),
    path("export/", ExpenseExportView.as_view(), name="expense-export"),
    path("import/preview/", ImportPreviewView.as_view(), name="expense-import-preview"),
    path("import/", ImportCommitView.as_view(), name="expense-import"),
    path("projects/", ProjectListCreateView.as_view(), name="project-list"),
    path("projects/<int:pk>/export/", ProjectExportView.as_view(), name="project-export"),
    path("projects/templates/", ProjectTemplatesView.as_view(), name="project-templates"),
    path("projects/<int:pk>/", ProjectDetailView.as_view(), name="project-detail"),
    path("projects/<int:project_id>/expenses/", ProjectExpenseListCreateView.as_view(), name="project-expense-list"),
    path("projects/<int:project_id>/expenses/bulk-delete/", ProjectExpenseBulkDeleteView.as_view(), name="project-expense-bulk-delete"),
    path("projects/<int:project_id>/expenses/<int:pk>/", ProjectExpenseDetailView.as_view(), name="project-expense-detail"),
]

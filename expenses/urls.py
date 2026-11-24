from django.urls import path

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
]

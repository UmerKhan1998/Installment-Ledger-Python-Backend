from django.contrib import admin

from .models import Expense, ExpenseSettings


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "amount", "currency", "category", "spent_on", "is_recurring")
    list_filter = ("category", "currency", "is_recurring")


admin.site.register(ExpenseSettings)


from .models import Project, ProjectExpense  # noqa: E402


class ProjectExpenseInline(admin.TabularInline):
    model = ProjectExpense
    extra = 0


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "currency", "budget", "status", "start_date", "end_date")
    inlines = [ProjectExpenseInline]

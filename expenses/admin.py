from django.contrib import admin

from .models import Expense, ExpenseSettings


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "amount", "currency", "category", "spent_on", "is_recurring")
    list_filter = ("category", "currency", "is_recurring")


admin.site.register(ExpenseSettings)

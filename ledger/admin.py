from django.contrib import admin

from .models import Payment, Plan


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("item", "user", "total_amount", "months", "first_payment_date")
    list_filter = ("months",)
    inlines = [PaymentInline]

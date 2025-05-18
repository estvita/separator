from django.contrib import admin
from .models import Tariff, Service, Trial


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_per_page = 50

@admin.register(Trial)
class TrialAdmin(admin.ModelAdmin):
    list_display = ("service", "owner")
    list_per_page = 50

@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_filter = ("service", "period", "is_trial")
    list_display = ("service", "duration", "price", "is_trial")
    list_per_page = 50
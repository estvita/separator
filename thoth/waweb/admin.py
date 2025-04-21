from django.contrib import admin
from .models import WaSession, WaServer

@admin.register(WaSession)
class WaSessionAdmin(admin.ModelAdmin):
    list_display = ('session', 'phone', 'date_end', 'status', 'owner')
    readonly_fields = ('session',)
    list_per_page = 30

@admin.register(WaServer)
class WaServerAdmin(admin.ModelAdmin):
    list_display = ('url', 'api_key')
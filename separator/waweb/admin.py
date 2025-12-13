from django.contrib import admin
from django.db import transaction
from django.contrib import messages
from .models import Session, Server
import separator.bitrix.utils as bitrix_utils


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner', 'server', 'line']
    list_display = ('session', 'server', 'phone', 'date_end', 'status', 'owner')
    search_fields = ("session", 'phone', "owner__email", "line__name")
    list_filter = ("status", "server")
    readonly_fields = ('session',)
    list_per_page = 30

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.phone and obj.app_instance:
            app_instance = obj.app_instance
            line_id = obj.line.id if obj.line else f"create__{app_instance.id}"

            def send_connect():
                try:
                    resp = bitrix_utils.connect_line(request, line_id, obj, "waweb")
                    messages.info(request, resp)
                except Exception as e:
                    messages.warning(request, f"Error: {e}")

            transaction.on_commit(send_connect)

@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ('url', 'api_key', 'max_connections', 'connected')
    search_fields = ['url']

    def connected(self, obj):
        return obj.sessions.filter(status='open').count()
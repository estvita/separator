from django.contrib import admin
from .models import WaSession, WaServer
from thoth.bitrix.models import Connector
import thoth.bitrix.utils as bitrix_utils

@admin.register(WaSession)
class WaSessionAdmin(admin.ModelAdmin):
    list_display = ('session', 'phone', 'date_end', 'status', 'owner')
    search_fields = ("session", 'phone')
    list_filter = ("status", )
    readonly_fields = ('session',)
    list_per_page = 30

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.app_instance:
            if obj.line:
                line_id = obj.line.id
            else:
                line_id = f"create__{obj.app_instance.id}"
            connector_service = "waweb"
            connector = Connector.objects.filter(service=connector_service).first()
            bitrix_utils.connect_line(request, line_id, obj, connector, connector_service)

@admin.register(WaServer)
class WaServerAdmin(admin.ModelAdmin):
    list_display = ('url', 'api_key')
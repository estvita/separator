from django.contrib import admin, messages
from django.db import transaction
import thoth.bitrix.utils as bitrix_utils

from .models import App, Waba, Phone, Template
from .tasks import call_management

@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("client_id", "verify_token")

@admin.register(Waba)
class WabaAdmin(admin.ModelAdmin):
    list_display = ("waba_id", "owner")
    list_per_page = 30

@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "lang", "owner", "waba", "status")
    list_per_page = 30


@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    list_display = ("phone_id", "phone", "owner", "waba", "line", "sms_service")
    search_fields = ("phone", "phone_id")
    list_filter = ("calling", )
    readonly_fields = ("error", )
    list_per_page = 30

    def save_model(self, request, obj, form, change):

        super().save_model(request, obj, form, change)

        transaction.on_commit(lambda: call_management.delay(obj.id))

        if obj.app_instance:
            app_instance = obj.app_instance
            line_id = obj.line.id if obj.line else f"create__{app_instance.id}"

            def send_connect():
                try:
                    resp = bitrix_utils.connect_line(request, line_id, obj, "waba")
                    messages.info(request, resp)
                except Exception as e:
                    messages.warning(request, f"Error: {e}")
            transaction.on_commit(send_connect)
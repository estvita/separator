from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import App, AppInstance, Bitrix, Line, AdminMessage, Connector
import thoth.bitrix.tasks as bitrix_tasks


class AdminMessageAdmin(admin.ModelAdmin):
    list_display = ('sent_at', 'message')
    fields = ('app_instance', 'message')
    filter_horizontal = ('app_instance',)
    list_per_page = 30

    def save_model(self, request, obj, form, change):
        # Save the object first to get an ID
        super().save_model(request, obj, form, change)
        app_instances = form.cleaned_data.get('app_instance')
        message = form.cleaned_data.get('message')
        for app_instance in app_instances:

            payload = {
                'USER_ID': app_instance.portal.user_id,
                'MESSAGE': message,
            }
            bitrix_tasks.call_api.delay(app_instance.id, "im.notify.system.add", payload)


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("name", "id", "site")
    search_fields = ("name",)


@admin.register(AppInstance)
class AppInstanceAdmin(admin.ModelAdmin):
    list_display = ("app", "owner", "portal_link", "status", "attempts")
    search_fields = ("id", "application_token")
    readonly_fields = ("app", "portal", 
                       "auth_status", "storage_id", "access_token", 
                       "refresh_token", "application_token", 
                       "status", "attempts")
    list_filter = ("app", "status")
    list_per_page = 30

    def portal_link(self, obj):
        if obj.portal:
            url = reverse("admin:bitrix_bitrix_change", args=[obj.portal.id])
            return format_html('<a href="{}">{}</a>', url, obj.portal.domain)
        return "-"
    portal_link.short_description = "Portal"


@admin.register(Bitrix)
class BitrixAdmin(admin.ModelAdmin):
    list_display = ("domain", "owner")
    search_fields = ("domain",)
    readonly_fields = ("domain", "client_endpoint", "user_id")
    list_per_page = 30

@admin.register(Connector)
class ConnectorAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "service")
    list_per_page = 30

@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    list_display = ("line_id", "app_instance", "owner")
    search_fields = ("line_id",)
    list_per_page = 30


admin.site.register(AdminMessage, AdminMessageAdmin)
from django.contrib import admin, messages
from django.db import transaction
from django.utils.html import format_html
from django.urls import reverse
import separator.bitrix.utils as bitrix_utils

from .models import App, Waba, Phone, Template, Event, Error
from .tasks import call_management

@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("client_id", "verify_token", "site", "api_version", "sip_server")

class TemplateInline(admin.TabularInline):
    model = Template
    extra = 0
    fields = ("template_link", "lang", "status")
    readonly_fields = ("id", "name", "template_link", "content", "lang", "status", "owner")

    def template_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_template_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.name)

class PhoneInline(admin.TabularInline):
    model = Phone
    extra = 0
    fields = ("phone_link", "phone_id")
    readonly_fields = ("phone_link", "phone_id")

    def phone_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_phone_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.phone)

@admin.register(Waba)
class WabaAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner']
    list_display = ("waba_id", "owner")
    search_fields = ["waba_id", "owner__email"]
    list_per_page = 30
    inlines = [PhoneInline, TemplateInline]

@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner']
    list_display = ("id", "name", "lang", "owner", "waba", "status")
    list_filter = ["status", "lang"]
    search_fields = ["waba__waba_id", "id", "name", "owner__email"]
    list_per_page = 30

@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner', 'waba', 'line', 'app_instance', 'sip_extensions']
    list_display = ("phone_id", "phone", "owner", "date_end", "type", "sms_service")
    search_fields = ("phone", "phone_id", "owner__email")
    list_filter = ("calling", "type")
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


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "waba")
    search_fields = ["waba__waba_id", "waba__app__client_id", "content"]
    list_per_page = 30
    list_filter = ("date",)


@admin.register(Error)
class ErrorAdmin(admin.ModelAdmin):
    list_display = ("code", "message")
    search_fields = ("code", "details", "message")
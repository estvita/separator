from django.contrib import admin, messages
from django.db import transaction, models
from django import forms
from django.utils.html import format_html
from django.urls import reverse
import separator.bitrix.utils as bitrix_utils
from separator.bitrix.models import AppInstance, Line

from .models import App, Waba, Phone, Template
from .tasks import call_management

@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("client_id", "verify_token", "api_version", "sip_server")

class TemplateInline(admin.TabularInline):
    model = Template
    extra = 0
    fields = ("template_link", "lang", "status", "owner")
    readonly_fields = ("template_link", "lang", "status", "owner")

    def template_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_template_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.name)

@admin.register(Waba)
class WabaAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner']
    list_display = ("waba_id", "owner")
    search_fields = ["waba_id", "owner__email"]
    list_per_page = 30
    inlines = [TemplateInline]

@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner']
    list_display = ("id", "name", "lang", "owner", "waba", "status")
    list_filter = ["status", "lang"]
    search_fields = ["waba__waba_id", "id", "name", "owner__email"]
    list_per_page = 30


class SessionForm(forms.ModelForm):
    class Meta:
        model = Phone
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        owner = self.instance.owner or self.initial.get('owner')

        app_instance_qs = AppInstance.objects.all()
        line_qs = Line.objects.all()

        if owner:
            app_instance_qs = app_instance_qs.filter(owner=owner)
            line_qs = line_qs.filter(owner=owner)

        if self.instance.pk:
            if self.instance.app_instance:
                app_instance_qs = AppInstance.objects.filter(
                    models.Q(pk=self.instance.app_instance.pk) | models.Q(owner=owner)
                )
            if self.instance.line:
                line_qs = Line.objects.filter(
                    models.Q(pk=self.instance.line.pk) | models.Q(owner=owner)
                )

        self.fields['app_instance'].queryset = app_instance_qs.distinct()
        self.fields['line'].queryset = line_qs.distinct()


@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    form = SessionForm
    autocomplete_fields = ['owner']
    list_display = ("phone_id", "phone", "owner", "date_end", "sip_extensions", "sms_service")
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
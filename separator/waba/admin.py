from django.contrib import admin, messages
from django.db import transaction
from django.utils.html import format_html
from django.urls import reverse
from django import forms
import separator.bitrix.utils as bitrix_utils

from .models import (
    App,
    Waba,
    Phone,
    Template,
    Event,
    Error,
    Ctwa,
    CtwaEvents,
    TemplateComponent,
    TemplateComponentButton,
    TemplateComponentNamedParam,
    TemplateComponentPositionalParam,
)
from .tasks import call_management

class AppAdminForm(forms.ModelForm):
    class Meta:
        model = App
        fields = '__all__'
        widgets = {
            'client_secret': forms.PasswordInput(render_value=True),
            'access_token': forms.PasswordInput(render_value=True),
        }

@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    form = AppAdminForm
    list_display = ("name", "client_id", "verify_token", "api_version")

class TemplateInline(admin.TabularInline):
    model = Template
    extra = 0
    fields = ("template_link", "lang", "status", "default")
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
    list_display = ("waba_id", "owner", "app", "subscribed")
    list_filter = ("subscribed", "app")
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
    readonly_fields = ("content",)
    inlines = []


class TemplateComponentInline(admin.TabularInline):
    model = TemplateComponent
    extra = 0
    fields = ("id_link", "type", "format", "text", "index")
    readonly_fields = ("id_link", "type", "format", "text", "index")

    def id_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_templatecomponent_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.pk)
    id_link.short_description = "ID"


class TemplateComponentButtonInline(admin.TabularInline):
    model = TemplateComponentButton
    extra = 0
    fields = ("id_link", "component", "type", "text", "url", "phone_number", "example", "index")
    readonly_fields = ("id_link", "component", "type", "text", "url", "phone_number", "example", "index")
    autocomplete_fields = ("component",)

    def id_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_templatecomponentbutton_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.pk)
    id_link.short_description = "ID"


class TemplateComponentNamedParamInline(admin.TabularInline):
    model = TemplateComponentNamedParam
    extra = 0
    fields = ("id_link", "component", "button", "name", "example")
    readonly_fields = ("id_link", "component", "button", "name", "example")
    autocomplete_fields = ("component", "button")

    def id_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_templatecomponentnamedparam_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.pk)
    id_link.short_description = "ID"


class TemplateComponentPositionalParamInline(admin.TabularInline):
    model = TemplateComponentPositionalParam
    extra = 0
    fields = ("id_link", "component", "button", "position", "example")
    readonly_fields = ("id_link", "component", "button", "position", "example")
    autocomplete_fields = ("component", "button")

    def id_link(self, instance):
        if not instance.pk:
            return "-"
        url = reverse("admin:waba_templatecomponentpositionalparam_change", args=[instance.pk])
        return format_html('<a href="{}">{}</a>', url, instance.pk)
    id_link.short_description = "ID"


TemplateAdmin.inlines = [
    TemplateComponentInline,
]


@admin.register(TemplateComponent)
class TemplateComponentAdmin(admin.ModelAdmin):
    list_display = ("id", "template", "type", "format", "index")
    list_filter = ("type", "format")
    search_fields = ("template__id", "template__name")
    autocomplete_fields = ("template",)
    list_per_page = 50
    inlines = [TemplateComponentButtonInline, TemplateComponentNamedParamInline, TemplateComponentPositionalParamInline]


@admin.register(TemplateComponentButton)
class TemplateComponentButtonAdmin(admin.ModelAdmin):
    list_display = ("id", "component", "type", "text", "index")
    list_filter = ("type",)
    search_fields = ("component__template__id", "component__template__name", "text")
    autocomplete_fields = ("component",)
    list_per_page = 50


@admin.register(TemplateComponentNamedParam)
class TemplateComponentNamedParamAdmin(admin.ModelAdmin):
    list_display = ("id", "component", "button", "name")
    search_fields = ("component__template__id", "component__template__name", "name")
    autocomplete_fields = ("component", "button")
    list_per_page = 50


@admin.register(TemplateComponentPositionalParam)
class TemplateComponentPositionalParamAdmin(admin.ModelAdmin):
    list_display = ("id", "component", "button", "position")
    search_fields = ("component__template__id", "component__template__name")
    autocomplete_fields = ("component", "button")
    list_per_page = 50

@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner', 'waba', 'line', 'app_instance', 'sip_extensions']
    list_display = ("phone_id", "phone", "owner", "waba_link", "date_end", "type")
    search_fields = ("phone", "phone_id", "owner__email")
    list_filter = ("calling", "type")
    readonly_fields = ("error", )
    list_per_page = 30

    def waba_link(self, instance):
        if not instance.waba_id:
            return "-"
        url = reverse("admin:waba_waba_change", args=[instance.waba_id])
        return format_html('<a href="{}">{}</a>', url, instance.waba.waba_id)
    waba_link.short_description = "WABA"

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
    list_display = ("code", "original", "fallback", "message")
    search_fields = ("code", "details", "message")
    list_filter = ("original", "fallback")


@admin.register(Ctwa)
class CtwaAdmin(admin.ModelAdmin):
    list_display = ("id", "waba_link")
    search_fields = ("id", "clid", "waba__id", "waba__waba_id")
    inlines = []

    def waba_link(self, instance):
        if not instance.waba_id:
            return "-"
        url = reverse("admin:waba_waba_change", args=[instance.waba_id])
        return format_html('<a href="{}">{}</a>', url, instance.waba.waba_id)
    waba_link.short_description = "WABA"


class CtwaEventsInline(admin.TabularInline):
    model = CtwaEvents
    extra = 0
    fields = ("date", "event")
    readonly_fields = ("date", "event")


CtwaAdmin.inlines = [CtwaEventsInline]

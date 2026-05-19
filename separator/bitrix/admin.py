import json

from django.contrib import admin, messages
from django import forms
from django.utils.html import format_html
from django.urls import reverse

from .models import (
    App,
    AppInstance,
    Bitrix,
    Line,
    ImNotify,
    Connector,
    User,
    Credential,
    Feature,
    FeatureGrant,
    ApiCall,
    Events,
)
from .crest import call_method
import separator.bitrix.tasks as bitrix_tasks


class AppInstanceInline(admin.TabularInline):
    model = AppInstance
    autocomplete_fields = ['owner']
    fields = ('instance_link', 'app', 'owner', 'auth_status', 'status')
    readonly_fields = ('instance_link', 'app', 'owner', 'auth_status', 'status')
    extra = 0
    can_delete = False

    def instance_link(self, obj):
        if obj.pk:
            url = reverse("admin:%s_appinstance_change" % obj._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, obj.id)
        return "-"
    instance_link.short_description = "ID"


class UserInline(admin.TabularInline):
    model = User
    autocomplete_fields = ['owner']
    fields = ('user_link', 'user_id', 'admin', 'active', 'owner')
    readonly_fields = ('user_link', 'user_id', 'admin', 'active', 'owner')
    extra = 0

    def user_link(self, obj):
        if obj.pk:
            url = reverse("admin:bitrix_user_change", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, obj.pk)
        return "-"
    user_link.short_description = "User"


class CredentialInline(admin.TabularInline):
    model = Credential
    fk_name = 'app_instance'
    extra = 0
    fields = ('credential', 'user', "refresh_date")
    readonly_fields = ('credential', 'user', "refresh_date")
    def credential(self, obj):
        if obj.pk:
            url = reverse("admin:bitrix_credential_change", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, obj.pk)
        return "-"


class CredentialUserInline(admin.TabularInline):
    model = Credential
    fk_name = 'user'
    extra = 0
    fields = ('credential', 'app_instance')
    readonly_fields = ('credential', 'app_instance')

    def credential(self, obj):
        if obj.pk:
            url = reverse("admin:bitrix_credential_change", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, obj.pk)
        return "-"
    
@admin.register(ImNotify)
class ImNotifyAdmin(admin.ModelAdmin):
    list_display = ('sent_at', 'message')
    fields = ('app', 'app_instance', 'message')
    autocomplete_fields = ['app_instance', 'app']
    list_per_page = 30

    def send_to_instance(self, instance, message):
        users = User.objects.filter(bitrix=instance.portal)
        for user in users:
            payload = {'USER_ID': user.user_id, 'MESSAGE': message}
            bitrix_tasks.call_api.delay(instance.id, "im.notify.system.add", payload, b24_user=user.id)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        message = form.cleaned_data['message']
        app = obj.app
        app_instance = obj.app_instance

        if app:
            for instance in AppInstance.objects.filter(app=app):
                self.send_to_instance(instance, message)
        elif app_instance:
            self.send_to_instance(app_instance, message)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        return form


class AppAdminForm(forms.ModelForm):
    class Meta:
        model = App
        fields = '__all__'
        widgets = {
            'client_secret': forms.PasswordInput(render_value=True),
        }


class FeatureGrantInline(admin.TabularInline):
    model = FeatureGrant
    extra = 0
    autocomplete_fields = ["feature"]
    fields = ("feature", "code", "date_end")


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    form = AppAdminForm
    list_display = ("name", "client_id", "site", "owner")
    search_fields = ("name", "id", "client_id", "owner__email")
    autocomplete_fields = ['owner']
    list_filter = ('autologin', 'asterx')
    fieldsets = (
        (None, {"fields": ("name", "save_events", "client_id", "client_secret", "site", "owner", "page_url")}),
        ("Bitrix", {"fields": ("handler", "events")}),
        ("Auth", {"fields": ("autologin", "min_version")}),
        ("Options", {"fields": ("connectors", "asterx", "vendor", "bitbot")}),
    )


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "apps_list", "method", "active")
    search_fields = ("name", "link", "method", "placements", "apps__name")
    list_filter = ("active",)
    filter_horizontal = ("apps",)
    actions = ("apply_now",)

    def apps_list(self, obj):
        return ", ".join(obj.apps.values_list("name", flat=True)) or "-"
    apps_list.short_description = "Apps"

    @admin.action(description="Применить сейчас")
    def apply_now(self, request, queryset):
        queued = 0
        for feature in queryset:
            bitrix_tasks.apply_feature_now.delay(feature.id)
            queued += 1

        self.message_user(
            request,
            f"Поставлено задач на применение фич: {queued}",
            level=messages.SUCCESS,
        )


@admin.register(FeatureGrant)
class FeatureGrantAdmin(admin.ModelAdmin):
    list_display = ("feature", "portal", "code", "date_end")
    search_fields = ("code", "feature__name", "portal__domain")
    autocomplete_fields = ["feature", "portal"]


@admin.register(Connector)
class ConnectorAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "service")


@admin.register(Bitrix)
class BitrixAdmin(admin.ModelAdmin):
    inlines = [UserInline, AppInstanceInline, FeatureGrantInline]
    autocomplete_fields = ['owner']
    list_display = ("domain", "owner", "license", "license_expired")
    search_fields = ("domain", "member_id", "owner__email")
    fields = ("protocol", "domain", "owner", "member_id", "license", "license_expired")
    list_filter = ('license_expired', "license")


@admin.register(AppInstance)
class AppInstanceAdmin(admin.ModelAdmin):
    inlines = [CredentialInline]
    autocomplete_fields = ['owner', "portal"]
    list_display = ("app", "owner", "portal_link", "status")
    search_fields = ("id", "application_token", "app__name", "portal__domain")
    list_filter = ("app", "status", "auth_status")

    def portal_link(self, obj):
        if obj.portal:
            url = reverse("admin:bitrix_bitrix_change", args=[obj.portal.id])
            return format_html('<a href="{}">{}</a>', url, obj.portal.domain)
        return "-"
    portal_link.short_description = "Portal"


@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner']
    list_display = ("line_id", "app_instance", "owner")
    search_fields = ("line_id", "portal__domain")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    inlines = [CredentialUserInline]
    list_display = ("id", "portal_link", "user_id", "admin", "active", "owner")
    list_filter = ("admin", "active")
    search_fields = ['user_id']

    def portal_link(self, obj):
        if obj.bitrix:
            url = reverse("admin:bitrix_bitrix_change", args=[obj.bitrix.id])
            return format_html('<a href="{}">{}</a>', url, obj.bitrix.domain)
        return "-"
    portal_link.short_description = "Portal"


class CredentialAdminForm(forms.ModelForm):
    class Meta:
        model = Credential
        fields = '__all__'
        widgets = {
            'access_token': forms.PasswordInput(render_value=True),
            'refresh_token': forms.PasswordInput(render_value=True),
        }


class ApiCallAdminForm(forms.ModelForm):
    class Meta:
        model = ApiCall
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        app = cleaned_data.get("app")
        app_instance = cleaned_data.get("app_instance")

        if app and app_instance:
            raise forms.ValidationError("Choose either app or app_instance, not both.")
        if not app and not app_instance:
            raise forms.ValidationError("app or app_instance is required.")

        return cleaned_data


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    form = CredentialAdminForm
    autocomplete_fields = ['user', 'app_instance']
    list_display = ("id", "app_instance", "user", "refresh_date")


@admin.register(ApiCall)
class ApiCallAdmin(admin.ModelAdmin):
    form = ApiCallAdminForm
    autocomplete_fields = ["app", "app_instance"]
    fields = ("app", "app_instance", "admin", "method", "payload")
    list_display = ("id", "app", "app_instance", "admin", "method")
    search_fields = ("method", "app__name", "app_instance__id", "app_instance__portal__domain")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        payload = obj.payload or {}
        try:
            if not obj.method:
                raise ValueError("method is required")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")

            if obj.app and not obj.app_instance:
                bitrix_tasks.dispatch_api_call.delay(obj.id)
                self.message_user(
                    request,
                    "Bitrix API call queued for app instances. Check Flower for per-instance results.",
                    level=messages.SUCCESS,
                )
                return

            if not obj.app_instance:
                raise ValueError("app_instance is required")

            call_kwargs = {"admin": True} if obj.admin else {}
            result = call_method(obj.app_instance, obj.method, payload, **call_kwargs)
            result_json = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            self.message_user(
                request,
                format_html("<pre style='white-space:pre-wrap'>{}</pre>", result_json),
                level=messages.SUCCESS,
            )
        except Exception as e:
            self.message_user(request, f"Bitrix API error: {e}", level=messages.ERROR)


@admin.register(Events)
class EventsAdmin(admin.ModelAdmin):
    list_display = ("id", "portal", "app")
    search_fields = ("portal__domain", "content")
    autocomplete_fields = ["portal", "app"]

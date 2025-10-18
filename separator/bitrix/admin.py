from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import App, AppInstance, Bitrix, Line, AdminMessage, Connector, User, Credential
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


class AdminMessageAdmin(admin.ModelAdmin):
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


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("name", "client_id", "site", "owner")
    search_fields = ("name", "id", "client_id", "owner__email")
    autocomplete_fields = ['owner']
    list_filter = ('autologin', 'asterx', 'imopenlines_auto_finish')
    list_per_page = 30


class CredentialInline(admin.TabularInline):
    model = Credential
    fk_name = 'app_instance'
    extra = 0
    fields = ('credential', 'user')
    readonly_fields = ('credential', 'user')
    def credential(self, obj):
        if obj.pk:
            url = reverse("admin:bitrix_credential_change", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, obj.pk)
        return "-"


@admin.register(AppInstance)
class AppInstanceAdmin(admin.ModelAdmin):
    inlines = [CredentialInline]
    autocomplete_fields = ['owner']
    list_display = ("app", "owner", "portal_link", "status")
    search_fields = ("id", "application_token", "app__name", "portal__domain")
    readonly_fields = ("auth_status", "storage_id", "application_token", 
                       "status")
    list_filter = ("app", "status", "auth_status")
    list_per_page = 30

    def portal_link(self, obj):
        if obj.portal:
            url = reverse("admin:bitrix_bitrix_change", args=[obj.portal.id])
            return format_html('<a href="{}">{}</a>', url, obj.portal.domain)
        return "-"
    portal_link.short_description = "Portal"


@admin.register(Bitrix)
class BitrixAdmin(admin.ModelAdmin):
    inlines = [UserInline, AppInstanceInline]    
    autocomplete_fields = ['owner']
    list_display = ("domain", "owner", "license_expired")
    search_fields = ("domain", "member_id")
    fields = ("protocol", "domain", "owner", "member_id", "license_expired")
    list_filter = ('license_expired', 'imopenlines_auto_finish')
    list_per_page = 30

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

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    inlines = [CredentialUserInline]
    list_display = ("id", "portal_link", "user_id", "admin", "active", "owner")
    list_filter = ("admin", "active")
    list_per_page = 30

    def portal_link(self, obj):
        if obj.bitrix:
            url = reverse("admin:bitrix_bitrix_change", args=[obj.bitrix.id])
            return format_html('<a href="{}">{}</a>', url, obj.bitrix.domain)
        return "-"
    portal_link.short_description = "Portal"


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    list_display = ("id", "app_instance", "user", "refresh_date")
    list_per_page = 30


@admin.register(Connector)
class ConnectorAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "service")
    list_per_page = 30

@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    autocomplete_fields = ['owner']
    list_display = ("line_id", "app_instance", "owner")
    search_fields = ("line_id",)
    list_per_page = 30


admin.site.register(AdminMessage, AdminMessageAdmin)
from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import App, AppInstance, Bitrix, Line, AdminMessage, Connector, User, Credential
import thoth.bitrix.tasks as bitrix_tasks


class AppInstanceInline(admin.TabularInline):
    model = AppInstance
    fields = ('instance_link', 'app', 'auth_status', 'status', 'attempts')
    readonly_fields = ('instance_link', 'app', 'auth_status', 'status', 'attempts')
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
    list_display = ('sent_at', 'message', 'app_instance')
    fields = ('app_instance', 'app_users', 'message')
    autocomplete_fields = ['app_instance']
    list_per_page = 30

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "app_users":
            obj_id = request.resolver_match.kwargs.get("object_id")
            app_instance_id = None

            if request.method == "POST":
                data = request.POST
                app_instance_id = data.get("app_instance")
            elif obj_id:
                app_instance_id = AdminMessage.objects.filter(pk=obj_id).values_list('app_instance', flat=True).first()

            if app_instance_id:
                try:
                    app_instance = AppInstance.objects.get(pk=app_instance_id)
                    kwargs["queryset"] = User.objects.filter(bitrix=app_instance.portal)
                except AppInstance.DoesNotExist:
                    kwargs["queryset"] = User.objects.none()
            else:
                kwargs["queryset"] = User.objects.none()

        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        app_instance = obj.app_instance
        app_users = form.cleaned_data.get('app_users')
        message = form.cleaned_data.get('message')

        for app_user in app_users:
            payload = {
                'USER_ID': app_user.user_id,
                'MESSAGE': message,
            }
            bitrix_tasks.call_api.delay(app_instance.id, "im.notify.system.add", payload)


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("name", "id", "client_id", "site")
    search_fields = ("name", "id", "client_id")
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
    list_display = ("app", "owner", "portal_link", "status", "attempts")
    search_fields = ("id", "application_token", "app__name", "portal__domain")
    readonly_fields = ("app", "portal",  "auth_status", "storage_id", "application_token", 
                       "status", "attempts")
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
    list_display = ("domain", "owner", "license_expired")
    search_fields = ("domain", "member_id")
    fields = ("protocol", "domain", "owner", "member_id", "license_expired")
    list_filter = ('license_expired',)
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
    list_display = ("line_id", "app_instance", "owner")
    search_fields = ("line_id",)
    list_per_page = 30


admin.site.register(AdminMessage, AdminMessageAdmin)
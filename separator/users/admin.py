from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import admin as auth_admin
from django.utils.translation import gettext_lazy as _

from .forms import UserAdminChangeForm
from .forms import UserAdminCreationForm
from .models import User, Message, SiteProfile

import os
from django.urls import reverse
from django.utils.html import format_html
from rest_framework.authtoken.models import Token

from separator.bitrix.models import Bitrix, AppInstance
from separator.waweb.models import Session
from separator.olx.models import OlxUser
from separator.waba.models import Phone


bases = (auth_admin.UserAdmin,)
if os.environ.get("DJANGO_SETTINGS_MODULE") == "config.settings.vendor":
    from hijack.contrib.admin import HijackUserAdminMixin
    bases = (HijackUserAdminMixin, auth_admin.UserAdmin)

if settings.DJANGO_ADMIN_FORCE_ALLAUTH:
    # Force the `admin` sign in process to go through the `django-allauth` workflow:
    # https://docs.allauth.org/en/latest/common/admin.html#admin
    admin.autodiscover()
    admin.site.login = secure_admin_login(admin.site.login)  # type: ignore[method-assign]


class OlxUserInline(admin.TabularInline):
    model = OlxUser
    extra = 0
    can_delete = False
    fields = ("olx_link", "date_end", "status")
    readonly_fields = ("olx_link", "date_end", "status")

    def olx_link(self, obj):
        if obj.id:
            url = reverse("admin:%s_olxuser_change" % obj._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, obj.olx_id)
        return ""
    
class PhoneInline(admin.TabularInline):
    model = Phone
    extra = 0
    can_delete = False
    fields = ("phone_link", "phone_id", "date_end")
    readonly_fields = ("phone_link", "phone_id", "date_end")

    def phone_link(self, obj):
        if obj.id:
            url = reverse("admin:%s_phone_change" % obj._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, obj.phone)
        return ""
    phone_link.short_description = "WABA"

class WaWebInline(admin.TabularInline):
    model = Session
    extra = 0
    can_delete = False
    fields = ("session_link", "date_end", "status")
    readonly_fields = ("session_link", "date_end", "status")

    def session_link(self, obj):
        if obj.id:
            url = reverse("admin:%s_session_change" % obj._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, obj.phone)
        return ""


class AppInstanceInline(admin.TabularInline):
    model = AppInstance
    extra = 0
    can_delete = False
    fields = ("admin_link", "status")
    readonly_fields = ("admin_link", "status")

    def admin_link(self, obj):
        if obj.id:
            url = reverse("admin:%s_appinstance_change" % obj._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, str(obj))
        return ""

class BitrixInline(admin.TabularInline):
    model = Bitrix
    extra = 0
    can_delete = False
    fields = ("admin_link",)
    readonly_fields = ("admin_link",)

    def admin_link(self, obj):
        if obj.id:
            url = reverse("admin:%s_bitrix_change" % Bitrix._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, obj.domain)
        return ""


@admin.register(Token)
class TokenAdmin(admin.ModelAdmin):
    list_display = ("key", "user", "created")
    search_fields = ("key", "user__email", "user__name")
    autocomplete_fields = ("user",)
    readonly_fields = ("key", "created")


@admin.register(User)
class UserAdmin(*bases):
    inlines = [BitrixInline, AppInstanceInline, WaWebInline, PhoneInline, OlxUserInline]
    form = UserAdminChangeForm
    add_form = UserAdminCreationForm
    readonly_fields = getattr(auth_admin.UserAdmin, "readonly_fields", ()) + ("token_link",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("site", "name", "phone_number", "integrator", "token_link")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    list_display = ["email", "name", "phone_number", "site", "integrator"]
    list_filter = ("integrator", "is_staff", "is_active", "is_superuser", "site")
    autocomplete_fields = ['site']
    list_per_page = 30
    search_fields = ["name", "email", "phone_number"]
    ordering = ["id"]
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )

    def token_link(self, obj):
        if not obj or not obj.pk:
            return "-"

        token = Token.objects.filter(user=obj).first()
        if not token:
            return "Token not created"

        url = reverse(
            f"admin:{Token._meta.app_label}_{Token._meta.model_name}_change",
            args=[token.pk],
        )
        return format_html('<a href="{}">{}</a>', url, token.key)

    token_link.short_description = "Token"


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "owner", "site")
    list_per_page = 30

@admin.register(SiteProfile)
class SiteProfileAdmin(admin.ModelAdmin):
    list_display = ("site", "owner")
    autocomplete_fields = ["site", "owner"]

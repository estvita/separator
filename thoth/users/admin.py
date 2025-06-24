from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import admin as auth_admin
from django.utils.translation import gettext_lazy as _

from .forms import UserAdminChangeForm
from .forms import UserAdminCreationForm
from .models import User, Message

from django.urls import reverse
from django.utils.html import format_html

from thoth.bitrix.models import Bitrix, AppInstance
from thoth.waweb.models import Session
from thoth.olx.models import OlxUser
from thoth.waba.models import Phone
from thoth.bot.models import Bot

if settings.DJANGO_ADMIN_FORCE_ALLAUTH:
    # Force the `admin` sign in process to go through the `django-allauth` workflow:
    # https://docs.allauth.org/en/latest/common/admin.html#admin
    admin.autodiscover()
    admin.site.login = secure_admin_login(admin.site.login)  # type: ignore[method-assign]


class BotInline(admin.TabularInline):
    model = Bot
    extra = 0
    can_delete = False
    fields = ("bot_link", "expiration_date")
    readonly_fields = ("bot_link", "expiration_date")

    def bot_link(self, obj):
        if obj.id:
            url = reverse("admin:%s_bot_change" % obj._meta.app_label, args=[obj.id])
            return format_html('<a href="{}">{}</a>', url, obj)
        return ""


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


@admin.register(User)
class UserAdmin(auth_admin.UserAdmin):
    inlines = [BitrixInline, AppInstanceInline, WaWebInline, PhoneInline, OlxUserInline, BotInline]
    form = UserAdminChangeForm
    add_form = UserAdminCreationForm
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("name", "phone_number", "integrator")}),
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
    list_display = ["email", "name", "phone_number", "is_superuser", "integrator"]
    list_filter = ("integrator", "is_staff", "is_active", "is_superuser")
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


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("code", "id")
    list_per_page = 30
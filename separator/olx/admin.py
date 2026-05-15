from django.contrib import admin
from django import forms

from .models import OlxApp
from .models import OlxAdvert
from .models import OlxCategory
from .models import OlxCategoryAttribute
from .models import OlxCity
from .models import OlxDistrict
from .models import OlxRegion
from .models import OlxThread
from .models import OlxUser

from separator.bitrix.models import Connector, Line
import separator.bitrix.utils as bitrix_utils

class OlxAppAdminForm(forms.ModelForm):
    class Meta:
        model = OlxApp
        fields = '__all__'
        widgets = {
            'client_secret': forms.PasswordInput(render_value=True),
        }


@admin.register(OlxApp)
class OlxAppAdmin(admin.ModelAdmin):
    form = OlxAppAdminForm
    list_display = ("name", "client_domain", "owner", "client_id")
    readonly_fields = ("authorization_link",)


class OlxUserAdminForm(forms.ModelForm):
    class Meta:
        model = OlxUser
        fields = '__all__'
        widgets = {
            'access_token': forms.PasswordInput(render_value=True),
            'refresh_token': forms.PasswordInput(render_value=True),
        }


@admin.register(OlxUser)
class OlxUserAdmin(admin.ModelAdmin):
    form = OlxUserAdminForm
    list_display = (
        "olx_id",
        "owner",
        "date_end",
        "status",
        "attempts",
    )
    autocomplete_fields = ['owner', 'line']
    search_fields = ("olx_id", 'line__name')
    list_filter = ("status", )
    readonly_fields = (
        "olx_id",
        "email",
        "name",
        "phone",
        "olxapp",
        "status",
        "attempts",
        # "line",
    )
    list_per_page = 30

    # def save_model(self, request, obj, form, change):
    #     super().save_model(request, obj, form, change)

        # ПОКА НЕТ ПРЯМОЙ ПРИВЯЗКИ к app_instance 
        # if obj.line:
        #     line_id = obj.line.line_id
        # else:
        #     line_id = f"create__{obj.app_instance.id}"
        # connector_service = "olx"
        # connector = Connector.objects.filter(service=connector_service).first()
        # bitrix_utils.connect_line(request, line_id, obj, connector, connector_service)


@admin.register(OlxRegion)
class OlxRegionAdmin(admin.ModelAdmin):
    list_display = ("name", "olx_id", "client_domain")
    list_filter = ("client_domain",)
    search_fields = ("name", "olx_id")
    list_per_page = 50


@admin.register(OlxCity)
class OlxCityAdmin(admin.ModelAdmin):
    list_display = ("name", "olx_id", "client_domain", "region", "latitude", "longitude")
    list_filter = ("client_domain", "region")
    search_fields = ("name", "olx_id", "region__name")
    autocomplete_fields = ("region",)
    list_per_page = 50


@admin.register(OlxDistrict)
class OlxDistrictAdmin(admin.ModelAdmin):
    list_display = ("name", "olx_id", "client_domain", "city", "latitude", "longitude")
    list_filter = ("client_domain",)
    search_fields = ("name", "olx_id", "city__name")
    autocomplete_fields = ("city",)
    list_per_page = 50


class OlxCategoryAttributeInline(admin.TabularInline):
    model = OlxCategoryAttribute
    extra = 0
    fields = ("code", "label", "unit", "validation", "values")
    show_change_link = True


@admin.register(OlxCategory)
class OlxCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "olx_id", "client_domain", "parent", "is_leaf", "photos_limit")
    list_filter = ("client_domain", "is_leaf")
    search_fields = ("name", "olx_id", "parent__name")
    autocomplete_fields = ("parent",)
    inlines = (OlxCategoryAttributeInline,)
    list_per_page = 50


@admin.register(OlxCategoryAttribute)
class OlxCategoryAttributeAdmin(admin.ModelAdmin):
    list_display = ("label", "code", "category", "attribute_type", "is_required")
    list_filter = ("category__client_domain",)
    search_fields = ("label", "code", "category__name", "category__olx_id")
    autocomplete_fields = ("category",)
    list_per_page = 50

    @admin.display(description="type")
    def attribute_type(self, obj):
        return obj.validation.get("type", "")

    @admin.display(boolean=True, description="required")
    def is_required(self, obj):
        return bool(obj.validation.get("required"))


@admin.register(OlxAdvert)
class OlxAdvertAdmin(admin.ModelAdmin):
    list_display = (
        "advert_id",
        "title",
        "olx_user",
        "status",
        "category",
        "city",
        "district",
        "pushup_enabled",
        "pushup_interval_days",
        "pushup_time",
        "next_pushup_at",
        "last_pushup_at",
    )
    list_filter = ("status", "pushup_enabled", "pushup_payment_method", "olx_user__olxapp__client_domain")
    search_fields = ("advert_id", "title", "olx_user__olx_id", "category__name", "city__name", "district__name")
    autocomplete_fields = ("olx_user", "category", "city", "district")
    readonly_fields = ("advert_id", "payload", "last_pushup_at", "last_pushup_error")
    list_per_page = 50


@admin.register(OlxThread)
class OlxThreadAdmin(admin.ModelAdmin):
    list_display = ("olx_user", "thread_id", "last_message_id", "total_count")
    search_fields = (
        "olx_user__olx_id",
        "olx_user__email",
        "olx_user__name",
        "thread_id",
        "last_message_id",
        "total_count",
    )
    autocomplete_fields = ("olx_user",)
    list_per_page = 50

from django.contrib import admin

from .models import OlxApp
from .models import OlxUser

from thoth.bitrix.models import Connector
import thoth.bitrix.utils as bitrix_utils

@admin.register(OlxApp)
class OlxAppAdmin(admin.ModelAdmin):
    list_display = ("name", "client_domain", "owner", "client_id")
    readonly_fields = ("authorization_link",)

@admin.register(OlxUser)
class OlxUserAdmin(admin.ModelAdmin):
    list_display = (
        "olx_id",
        "owner",
        "date_end",
        "status",
        "attempts",
    )
    search_fields = ("olx_id", "email", "name", "phone")
    readonly_fields = (
        "access_token",
        "refresh_token",
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
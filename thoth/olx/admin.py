from django.contrib import admin
from django import forms

from .models import OlxApp
from .models import OlxUser

from thoth.bitrix.models import Connector, Line
import thoth.bitrix.utils as bitrix_utils

class UsersForm(forms.ModelForm):
    class Meta:
        model = OlxUser
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        owner = self.instance.owner or self.initial.get('owner')
        if owner:
            self.fields['line'].queryset = Line.objects.filter(owner=owner)

@admin.register(OlxApp)
class OlxAppAdmin(admin.ModelAdmin):
    list_display = ("name", "client_domain", "owner", "client_id")
    readonly_fields = ("authorization_link",)

@admin.register(OlxUser)
class OlxUserAdmin(admin.ModelAdmin):
    form = UsersForm
    list_display = (
        "olx_id",
        "owner",
        "date_end",
        "status",
        "attempts",
    )
    search_fields = ("olx_id", )
    list_filter = ("status", )
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
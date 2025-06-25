from django.contrib import admin
from .models import Session, Server
from thoth.bitrix.models import Connector, AppInstance, Line
import thoth.bitrix.utils as bitrix_utils
from django.contrib import messages
from django import forms


class SessionForm(forms.ModelForm):
    class Meta:
        model = Session
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        owner = self.instance.owner or self.initial.get('owner')
        if owner:
            self.fields['app_instance'].queryset = AppInstance.objects.filter(owner=owner)
            self.fields['line'].queryset = Line.objects.filter(owner=owner)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    form = SessionForm
    list_display = ('session', 'server', 'phone', 'date_end', 'status', 'owner')
    search_fields = ("session", 'phone')
    list_filter = ("status", "server")
    readonly_fields = ('session',)
    list_per_page = 30

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.app_instance:
            line_id = obj.line.id if obj.line else f"create__{obj.app_instance.id}"
            connector_service = "waweb"
            connector = Connector.objects.filter(service=connector_service).first()
            try:
                resp = bitrix_utils.connect_line(request, line_id, obj, connector, connector_service)
                messages.info(request, f"Ответ Bitrix24: {resp}")
            except Exception as e:
                messages.warning(request, f"Ответ Bitrix24: {e}")

@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ('url', 'api_key', 'max_connections', 'connected')

    def connected(self, obj):
        return obj.sessions.count()
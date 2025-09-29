from django.contrib import admin
from django.db import transaction, models
from django.contrib import messages
from django import forms
from .models import Session, Server
from thoth.bitrix.models import Connector, AppInstance, Line
import thoth.bitrix.utils as bitrix_utils


class SessionForm(forms.ModelForm):
    class Meta:
        model = Session
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        owner = self.instance.owner or self.initial.get('owner')

        app_instance_qs = AppInstance.objects.all()
        line_qs = Line.objects.all()

        if owner:
            app_instance_qs = app_instance_qs.filter(owner=owner)
            line_qs = line_qs.filter(owner=owner)

        if self.instance.pk:
            if self.instance.app_instance:
                app_instance_qs = AppInstance.objects.filter(
                    models.Q(pk=self.instance.app_instance.pk) | models.Q(owner=owner)
                )
            if self.instance.line:
                line_qs = Line.objects.filter(
                    models.Q(pk=self.instance.line.pk) | models.Q(owner=owner)
                )

        self.fields['app_instance'].queryset = app_instance_qs.distinct()
        self.fields['line'].queryset = line_qs.distinct()


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
        if obj.phone and obj.app_instance:
            app_instance = obj.app_instance
            line_id = obj.line.id if obj.line else f"create__{app_instance.id}"

            def send_connect():
                try:
                    resp = bitrix_utils.connect_line(request, line_id, obj, "waweb")
                    messages.info(request, resp)
                except Exception as e:
                    messages.warning(request, f"Error: {e}")

            transaction.on_commit(send_connect)

@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ('url', 'api_key', 'max_connections', 'connected')

    def connected(self, obj):
        return obj.sessions.filter(status='open').count()
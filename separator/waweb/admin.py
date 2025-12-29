from django.contrib import admin
from django.db import transaction
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django import forms
import requests
from .models import Session, Server
import separator.bitrix.utils as bitrix_utils


class SessionAdminForm(forms.ModelForm):
    class Meta:
        model = Session
        fields = '__all__'
        widgets = {
            'apikey': forms.PasswordInput(render_value=True),
        }


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    form = SessionAdminForm
    autocomplete_fields = ['owner', 'server', 'line']
    list_display = ('session', 'server', 'phone', 'date_end', 'status', 'owner')
    search_fields = ("session", 'phone', "owner__email", "line__name")
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

class ServerForm(forms.ModelForm):
    url = forms.CharField(label="Server URL", help_text="http://evolution:8080")

    class Meta:
        model = Server
        fields = '__all__'
        widgets = {
            'api_key': forms.PasswordInput(render_value=True),
        }

    def clean(self):
        cleaned_data = super().clean()
        url = cleaned_data.get("url")
        api_key = cleaned_data.get("api_key")

        if url and api_key:
            try:
                # Проверка соединения с Evolution API
                headers = {"apikey": api_key}
                response = requests.get(url, headers=headers, timeout=5)
                
                if response.status_code != 200:
                    raise ValidationError(f"Connection error: {response.status_code} {response.text}")
                
                data = response.json()
                self.evolution_data = data
                
            except requests.RequestException as e:
                raise ValidationError(f"Failed to connect to server: {str(e)}")

        return cleaned_data

@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    form = ServerForm
    list_display = ('url', 'api_key', 'max_connections', 'connected')
    search_fields = ['url']
    fieldsets = (
        (None, {
            'fields': ('url', 'api_key', 'max_connections')
        }),
        (_('Default settings for new sessions'), {
            'fields': ('groups_ignore', 'always_online', 'read_messages', 'sync_history'),
            'description': _('These settings will be applied by default when creating new sessions.')
        }),
    )

    def connected(self, obj):
        return obj.sessions.filter(status='open').count()

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if hasattr(form, 'evolution_data'):
            messages.success(request, f"Server connected successfully. Data: {form.evolution_data}")
from django.contrib import admin
from django import forms
from .models import Server, Extension

# Register your models here.
class ServerAdminForm(forms.ModelForm):
    class Meta:
        model = Server
        fields = '__all__'
        widgets = {
            'client_id': forms.PasswordInput(render_value=True),
            'client_secret': forms.PasswordInput(render_value=True),
        }

@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    form = ServerAdminForm
    list_display = ('domain', 'sip_port', 'gql_scopes')
    list_per_page = 30


class ExtensionAdminForm(forms.ModelForm):
    class Meta:
        model = Extension
        fields = '__all__'
        widgets = {
            'password': forms.PasswordInput(render_value=True),
        }

@admin.register(Extension)
class ExtensionAdmin(admin.ModelAdmin):
    form = ExtensionAdminForm
    autocomplete_fields = ['owner']
    search_fields = ['number', 'server__domain']
    list_display = ('number', 'date_end', 'owner')
    list_per_page = 30
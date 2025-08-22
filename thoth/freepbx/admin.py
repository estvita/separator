from django.contrib import admin
from .models import Server, Extension

# Register your models here.
@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ('domain', 'sip_port', 'gql_scopes')
    list_per_page = 30


@admin.register(Extension)
class ExtensionAdmin(admin.ModelAdmin):
    list_display = ('number', 'date_end', 'owner')
    list_per_page = 30
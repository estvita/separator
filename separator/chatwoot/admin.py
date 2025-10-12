from django.contrib import admin
from .models import *

@admin.register(Chatwoot)
class ChatwootAdmin(admin.ModelAdmin):
    list_display = ("url", "api_version",)

@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "is_enabled",)

@admin.register(Limit)
class LimitAdmin(admin.ModelAdmin):
    list_display = ("name", "value",)

@admin.register(AgentBot)
class AgentBottAdmin(admin.ModelAdmin):
    list_display = ("id", "account",)
    list_per_page = 50

@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("id", "owner")
    list_per_page = 50

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "owner")
    list_per_page = 50

@admin.register(Inbox)
class InboxAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "owner")
    list_per_page = 50
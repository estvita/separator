from django.contrib import admin
from .models import Provider, Connector, ChatBot, Command, CommandLang

class ConnectorInline(admin.TabularInline):
    model = Connector
    extra = 0
    show_change_link = True
    per_page = 50 

class ChatBotInline(admin.TabularInline):
    model = ChatBot
    extra = 0
    show_change_link = True

class CommandInline(admin.TabularInline):
    model = Command
    extra = 0
    show_change_link = True

class CommandLangInline(admin.TabularInline):
    model = CommandLang
    extra = 0
    show_change_link = True

@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ['id', 'type', 'url']
    search_fields = ['id', 'type', 'url']
    inlines = [ConnectorInline]
    list_per_page = 50

@admin.register(Connector)
class ConnectorAdmin(admin.ModelAdmin):
    list_display = ['id', 'owner', 'provider', 'url', 'key']
    search_fields = ['id', 'owner__email', 'provider__type', 'url', 'key']
    list_filter = ['provider']
    inlines = [ChatBotInline]
    autocomplete_fields = ['owner', 'provider']

@admin.register(ChatBot)
class ChatBotAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'owner', 'connector', 'bot_id', 'bot_type']
    search_fields = ['id', 'name', 'owner__email', 'connector__url', 'bot_id']
    inlines = [CommandInline]
    autocomplete_fields = ['owner', 'connector', 'app_instance']

@admin.register(Command)
class CommandAdmin(admin.ModelAdmin):
    list_display = ['id', 'command', 'bot', 'command_id', 'common', 'hidden', 'extranet']
    search_fields = ['id', 'command', 'bot__name']
    inlines = [CommandLangInline]
    autocomplete_fields = ['bot']

@admin.register(CommandLang)
class CommandLangAdmin(admin.ModelAdmin):
    list_display = ['id', 'language', 'title', 'params', 'command']
    search_fields = ['id', 'language', 'title', 'params', 'command__command']
    autocomplete_fields = ['command']
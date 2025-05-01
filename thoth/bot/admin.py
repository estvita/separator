from django.contrib import admin
from django import forms
from .models import Provider, Model, ApiKey, Bot, Feature, Voice, Vocal

@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)

@admin.register(Model)
class ModelAdmin(admin.ModelAdmin):
    list_display = ("name", "provider")
    list_filter = ("provider",)
    list_per_page = 50

@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "provider")
    list_filter = ("provider",)
    list_per_page = 50


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "engine", "type", "privacy", "owner")
    list_per_page = 50


@admin.register(Vocal)
class VocalAdmin(admin.ModelAdmin):
    list_per_page = 50


class BotAdminForm(forms.ModelForm):
    class Meta:
        model = Bot
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['features'].queryset = Feature.objects.filter(engine='text')


@admin.register(Bot)
class BotAdmin(admin.ModelAdmin):
    form = BotAdminForm
    list_display = ("id", "name", "expiration_date", "agent_bot", "owner")
    list_filter = ("model", )
    list_per_page = 50


# Форма для модели Voice — фильтруем функции по engine="voice"
class VoiceAdminForm(forms.ModelForm):
    class Meta:
        model = Voice
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['features'].queryset = Feature.objects.filter(engine='voice')


@admin.register(Voice)
class VoiceAdmin(admin.ModelAdmin):
    form = VoiceAdminForm
    list_display = ("id", "name", "expiration_date", "owner")
    fields = ("id", "password", "name", "expiration_date", "owner",
              "token", "model", "vocal", "instruction", "welcome_msg", "features",
              "transfer_uri", "temperature", "max_tokens")
    list_filter = ("model", "owner")
    list_per_page = 50

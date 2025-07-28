from django.contrib import admin
from django import forms
from .models import Server, Context, Settings

class ServerForm(forms.ModelForm):
    class Meta:
        model = Server
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        owner = self.instance.owner or self.initial.get('owner')
        if owner:
            self.fields['settings'].queryset = Settings.objects.filter(app_instance__owner=owner)

class ContextInline(admin.TabularInline):
    model = Context
    extra = 0

@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    form = ServerForm
    inlines = [ContextInline]
    list_display = ('name', 'date_end', 'settings', 'owner', 'setup_complete')
    search_fields = ("id", 'entity_id', 'pbx_uuid')
    readonly_fields = ('version', 'system', 'entity_id', 'pbx_uuid')
    list_filter = ("setup_complete", )
    list_per_page = 30


admin.site.register(Settings)
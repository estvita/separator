from django.contrib import admin

# Register your models here.
from .models import Dify

@admin.register(Dify)
class DifyAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "expiration_date", "owner", "base_url")
    list_filter = ("type",)
    list_per_page = 50
from django.contrib import admin

# Register your models here.
from .models import ArticlePage

@admin.register(ArticlePage)
class ArticlePageAdmin(admin.ModelAdmin):
    list_display = ("title",)
    list_per_page = 30
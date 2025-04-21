from django.urls import path
from .views import voice_list_view, voice_form_view, voice_delete

app_name = "voice"

urlpatterns = [
    path('', voice_list_view, name='voice_list'),
    path('new/', voice_form_view, name='voice_new'),
    path('<str:voice_id>/', voice_form_view, name='voice_edit'),
    path('<str:voice_id>/delete/', voice_delete, name='voice_delete'),
]

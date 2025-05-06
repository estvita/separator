from django.urls import path
from thoth.bot import views

app_name = "voice"

urlpatterns = [
    path('', views.voice_list_view, name='voice_list'),
    path('new/', views.voice_form_view, name='voice_new'),

    # --- функции ДО любых slug/id путей! ---
    path('features/', views.feature_list_view, name='feature_list'),
    path('features/new/', views.feature_form_view, name='feature_new'),
    path('features/<int:feature_id>/', views.feature_form_view, name='feature_edit'),
    path('features/<int:feature_id>/delete/', views.feature_delete_view, name='feature_delete'),

    path('<str:voice_id>/', views.voice_form_view, name='voice_edit'),
    path('<str:voice_id>/delete/', views.voice_delete, name='voice_delete'),
]

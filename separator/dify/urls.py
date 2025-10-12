from django.urls import path
from .views import dify_form_view, dify_list_view, dify_delete

app_name = "dify"

urlpatterns = [
    path('', dify_list_view, name='dify_list'),
    path('<int:dify_id>/', dify_form_view, name='dify_edit'),
    path('new/', dify_form_view, name='dify_new'),
    path('<int:dify_id>/delete/', dify_delete, name='dify_delete'),
]

from django.urls import path
from . import views

urlpatterns = [
    path('', views.server_list, name='asterx'),
    path('server/<uuid:server_id>/edit/', views.edit_asterx, name='edit_asterx'),
    path('settings/<int:id>/', views.app_settings, name='app_settings'),

]
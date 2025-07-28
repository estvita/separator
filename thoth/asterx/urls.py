from django.urls import path
from .views import server_list, edit_asterx

urlpatterns = [
    path('', server_list, name='asterx'),
    path('server/<uuid:server_id>/edit/', edit_asterx, name='edit_asterx'),
]
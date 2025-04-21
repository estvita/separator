from django.urls import path
from . import views

app_name = "waweb"
urlpatterns = [
    path('', views.wa_sessions, name='wa_sessions'),
    path('connect/', views.connect_number, name='connect_number'),
    path('qr/<uuid:session_id>/', views.qr_code_page, name='qr_code_page'),
    path('send_message/<uuid:session_id>/', views.send_message_view, name='send_message'),
]

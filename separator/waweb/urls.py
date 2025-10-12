from django.urls import path
from . import views

urlpatterns = [
    path('', views.wa_sessions, name='waweb'),
    path('connect/', views.connect_number, name='connect_number'),
    path('qr/<uuid:session_id>/', views.qr_code_page, name='qr_code_page'),
    path('shareqr/<uuid:public_id>/', views.share_qr, name='share_qr'),
    path('send_message/<uuid:session_id>/', views.send_message_view, name='send_message'),
]

from django.urls import path
from . import views

urlpatterns = [
    path('', views.waba_view, name='waba'),
    path('callback/', views.facebook_callback, name='facebook_callback'),
    path('request/', views.save_request, name='save_request'),
    path('phone/<int:phone_id>/', views.phone_details, name='phone-details'),
]
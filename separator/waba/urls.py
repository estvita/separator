from django.urls import path
from . import views
from .api import views as api_views

urlpatterns = [
    path('', views.waba_view, name='waba'),
    path('partner/', views.partner_apps, name='waba-partner'),
    path('partner/<uuid:partner_app_id>/', views.partner_app_edit, name='waba-partner-edit'),
    path('callback/', views.facebook_callback, name='facebook_callback'),
    path('es-link/', api_views.embedded_signup_link, name='embedded_signup_link'),
    path('popup-callback/', api_views.embedded_signup_popup_callback, name='embedded_signup_popup_callback'),
    path('popup-message/', api_views.embedded_signup_popup_message, name='embedded_signup_popup_message'),
    path('phone/<str:phone_id>/', views.phone_details, name='phone-details'),
    path('account/<str:waba_id>/', views.waba_account_details, name='waba-account-details'),
    path('broadcast/', views.broadcast_page, name='broadcast-page'),
    path('broadcast/<int:broadcast_id>/', views.broadcast_details, name='broadcast-details'),
    path('interactive/', views.interactive_messages, name='waba-interactive'),
    path('interactive/create/', views.interactive_message_create, name='waba-interactive-create'),
    path('interactive/<uuid:message_id>/edit/', views.interactive_message_edit, name='waba-interactive-edit'),
    path('interactive/<uuid:message_id>/delete/', views.interactive_message_delete, name='waba-interactive-delete'),
]

from django.urls import path

from .api.views import SmsViewSet
from .views import portals, app_settings, app_install, portal_detail

urlpatterns = [
    path("api/bitrix/sms/", SmsViewSet.as_view({"post": "create"}), name="sms"),
    path("portals/", portals, name="portals"),
    path("portals/<int:portal_id>/", portal_detail, name="portal_detail"),
    path("app-settings/", app_settings, name="app_settings"),
    path("app-install/", app_install, name="app_install"),
    path("api/bitrix/placement/", app_settings, name="app_settings"), # временно
]
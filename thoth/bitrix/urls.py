from django.urls import path

from .api.views import SmsViewSet
from .views import portals, app_settings, link_user

urlpatterns = [
    path("api/bitrix/sms/", SmsViewSet.as_view({"post": "create"}), name="sms"),
    path("portals/", portals, name="portals"),
    path("app-settings/", app_settings, name="app_settings"),
    path("api/bitrix/placement/", app_settings, name="app_settings"), # временно
    path('link-user/', link_user, name='link_user'),
]

from django.urls import path

from .api.views import SmsViewSet, BizprocViewSet, ConversionViewSet
from .views import portals, app_settings, app_install, portal_detail, process_placement

urlpatterns = [
    path("api/bitrix/sms/", SmsViewSet.as_view({"post": "create"}), name="sms"),
    path("api/bitrix/bizproc/", BizprocViewSet.as_view({"post": "create"}), name="bizproc"),
    path("api/bitrix/conversion/", ConversionViewSet.as_view({"post": "create"}), name="conversion"),
    path("api/bitrix/placement/", process_placement, name="process_placement_legacy"), # Legacy endpoint for placements, can be removed in the future
    path("portals/", portals, name="portals"),
    path("portals/<int:portal_id>/", portal_detail, name="portal_detail"),
    path("app-settings/", app_settings, name="app_settings"),
    path("app-install/", app_install, name="app_install"),
    path("placement/", process_placement, name="process_placement"),
]
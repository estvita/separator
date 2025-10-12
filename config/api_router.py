from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from separator.bitrix.api.views import PortalViewSet
from separator.users.api.views import UserViewSet
from separator.waba.api.views import WabaWebhook
from separator.waweb.api.views import EventsHandler
from separator.dify.api.views import DifyReceiver
from separator.asterx.api.views import AsterxHandler
from separator.freepbx.api.views import ExtViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

# router.register("users", UserViewSet)
router.register("bitrix", PortalViewSet)
router.register("waba", WabaWebhook)
router.register("dify", DifyReceiver, basename="dify")
router.register("waweb", EventsHandler, basename="waevents")
router.register("asterx", AsterxHandler, basename="asterx")
router.register("ext", ExtViewSet, basename="ext")

app_name = "api"
urlpatterns = router.urls
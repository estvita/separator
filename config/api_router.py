from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from thoth.bitrix.api.views import PortalViewSet
from thoth.users.api.views import UserViewSet
from thoth.waba.api.views import WabaWebhook
from thoth.waweb.api.views import EventsHandler
from thoth.bot.api.views import BotHandler, VoiceDetails
from thoth.dify.api.views import DifyReceiver
from thoth.asterx.api.views import AsterxHandler

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("users", UserViewSet)
router.register("bitrix", PortalViewSet)
router.register("waba", WabaWebhook)
router.register("dify", DifyReceiver, basename="dify")
router.register("bot", BotHandler, basename="bot")
# router.register("voice", VoiceDetails, basename="voice")
router.register("waweb", EventsHandler, basename="waevents")
router.register("asterx", AsterxHandler, basename="asterx")

app_name = "api"
urlpatterns = router.urls
# asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
import thoth.asterx.routing

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": URLRouter(
        thoth.asterx.routing.websocket_urlpatterns
    ),
})
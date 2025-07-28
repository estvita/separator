# routing.py
from django.urls import re_path
from .consumers import ServerAuthConsumer

websocket_urlpatterns = [
    re_path(r'ws/asterx/$', ServerAuthConsumer.as_asgi()),
]
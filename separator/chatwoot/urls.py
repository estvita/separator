from django.urls import path
from .views import chat_sso_redirect

urlpatterns = [
    path("", chat_sso_redirect, name="chat_sso_redirect"),
]
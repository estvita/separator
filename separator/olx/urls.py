from django.urls import path

from .api.views import OlxAuthorizationAPIView
from .views import olx_accounts

urlpatterns = [
    path("olx/accounts/", olx_accounts, name="olx-accounts"),
    path(
        "api/olx/authorization/",
        OlxAuthorizationAPIView.as_view(),
        name="olx-authorization",
    ),
]

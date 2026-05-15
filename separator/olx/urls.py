from django.urls import path

from .api.views import OlxAuthorizationAPIView
from .views import olx_account_adverts, olx_accounts

urlpatterns = [
    path("olx/accounts/", olx_accounts, name="olx-accounts"),
    path("olx/accounts/<int:account_id>/adverts/", olx_account_adverts, name="olx-account-adverts"),
    path(
        "api/olx/authorization/",
        OlxAuthorizationAPIView.as_view(),
        name="olx-authorization",
    ),
]

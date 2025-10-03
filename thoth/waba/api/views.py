# waba/views.py
import logging

from django.http import HttpResponse
from rest_framework.mixins import CreateModelMixin
from rest_framework.viewsets import GenericViewSet

from thoth.waba.models import App, Waba, Phone
from thoth.waba.utils import message_processing

logger = logging.getLogger("waba")


class WabaWebhook(GenericViewSet, CreateModelMixin):
    queryset = Phone.objects.all()

    def create(self, request, *args, **kwargs):
        data = request.data
        message_processing.delay(data)
        return HttpResponse("ok")

    def list(self, request, *args, **kwargs):
        hub_mode = request.query_params.get("hub.mode")
        hub_challenge = request.query_params.get("hub.challenge")
        hub_verify_token = request.query_params.get("hub.verify_token")

        if hub_mode == "subscribe" and hub_verify_token:
            try:
                app = App.objects.get(
                    verify_token=hub_verify_token,
                    # owner=request.user.id,
                )
                return HttpResponse(hub_challenge, content_type="text/plain")
            except App.DoesNotExist:
                logger.error(
                    f"Verification token not found or does not belong to the user {request.query_params}",
                )
                return HttpResponse(
                    "token not found",
                    status=403,
                    content_type="text/plain",
                )
        return HttpResponse("Bad Request", status=400, content_type="text/plain")

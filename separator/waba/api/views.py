# waba/views.py
import logging
import hmac
import hashlib

from django.http import HttpResponse
from rest_framework.mixins import CreateModelMixin
from rest_framework.viewsets import GenericViewSet
from rest_framework.permissions import AllowAny

from separator.waba.models import App, Waba, Phone
from separator.waba.utils import event_processing

logger = logging.getLogger("waba")


class WabaWebhook(GenericViewSet, CreateModelMixin):
    queryset = Phone.objects.all()
    authentication_classes = []
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        signature = request.headers.get("X-Hub-Signature-256")
        app_id = request.query_params.get('app_id')
        host = request.get_host()
        
        # Pass raw body for signature verification in the task
        raw_body = request.body.decode('utf-8')
        
        event_processing.delay(
            raw_body=raw_body, 
            signature=signature, 
            app_id=app_id, 
            host=host
        )
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

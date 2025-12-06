from rest_framework.mixins import CreateModelMixin
from rest_framework.renderers import JSONRenderer
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.contrib import messages
from django.shortcuts import redirect
from urllib.parse import urlparse

from separator.bitrix.models import Bitrix
import separator.bitrix.utils as utils


class PortalViewSet(CreateModelMixin, GenericViewSet):
    queryset = Bitrix.objects.all()
    authentication_classes = []
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        utils.event_processor.delay(request.data)
        return Response("ok")

    def head(self, request, *args, **kwargs):
        return Response(headers={'Allow': 'POST, HEAD'})
    
    def get(self, request, *args, **kwargs):
        url = request.GET.get("url")
        if url:
            parsed = urlparse(url)
            if parsed.scheme in ["http", "https"] and parsed.netloc:
                domain = parsed.netloc.split(':')[0]
                if Bitrix.objects.filter(domain=domain).exists():
                    return redirect(url)
                else:
                    messages.error(request, f'Domain "{domain}" not found: {url}')
                    return redirect("/")
        messages.error(request, f'Incorrect url parameter: "{url}"')
        return redirect("/")


class SmsViewSet(GenericViewSet, CreateModelMixin):
    renderer_classes = [JSONRenderer]
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        return Bitrix.objects.none()

    def create(self, request, *args, **kwargs):
        service = request.query_params.get('service')
        utils.sms_processor.delay(request.data, service)
        return Response("ok")


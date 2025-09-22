from rest_framework.mixins import CreateModelMixin
from rest_framework.renderers import JSONRenderer
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response

from thoth.bitrix.models import Bitrix
import thoth.bitrix.utils as utils

from .serializers import PortalSerializer


class PortalViewSet(CreateModelMixin, GenericViewSet):
    queryset = Bitrix.objects.all()
    serializer_class = PortalSerializer

    def create(self, request, *args, **kwargs):
        data = request.data
        app_id = request.query_params.get("app-id")
        utils.event_processor.delay(data, app_id, request.user.id)
        return Response("ok")

    def head(self, request, *args, **kwargs):
        return Response(headers={'Allow': 'POST, HEAD'})


class SmsViewSet(GenericViewSet, CreateModelMixin):
    renderer_classes = [JSONRenderer]

    def get_queryset(self):
        return Bitrix.objects.none()

    def create(self, request, *args, **kwargs):
        service = request.query_params.get('service')
        data = request.data
        utils.sms_processor.delay(data, service)
        return Response("ok")


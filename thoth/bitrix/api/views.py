from rest_framework.mixins import CreateModelMixin
from rest_framework.renderers import JSONRenderer
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response

from thoth.bitrix.models import Bitrix

from ..utils import event_processor
from ..utils import sms_processor
from .serializers import PortalSerializer


class PortalViewSet(CreateModelMixin, GenericViewSet):
    queryset = Bitrix.objects.all()
    serializer_class = PortalSerializer

    def create(self, request, *args, **kwargs):
        return event_processor(request)

    def head(self, request, *args, **kwargs):
        return Response(headers={'Allow': 'POST, HEAD'})


class SmsViewSet(GenericViewSet, CreateModelMixin):
    renderer_classes = [JSONRenderer]

    def get_queryset(self):
        return Bitrix.objects.none()

    def create(self, request, *args, **kwargs):
        return sms_processor(request)

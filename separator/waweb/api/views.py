import logging
import socket
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.permissions import BasePermission, IsAuthenticated
from separator.waweb.tasks import event_processor


logger = logging.getLogger("django")


class IsEvolutionService(BasePermission):
    def has_permission(self, request, view):
        try:
            evolution_ip = socket.gethostbyname('evolution')
            return request.META.get('REMOTE_ADDR') == evolution_ip
        except Exception:
            return False


class EventsHandler(GenericViewSet):
    permission_classes = [IsAuthenticated | IsEvolutionService]

    def create(self, request, *args, **kwargs):
        event_data = request.data
        event_processor.delay(event_data)            
        return Response("ok")
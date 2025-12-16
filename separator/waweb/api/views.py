import logging
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from separator.waweb.tasks import event_processor


logger = logging.getLogger("django")


class EventsHandler(GenericViewSet):
    def create(self, request, *args, **kwargs):
        event_data = request.data
        event_processor.delay(event_data)            
        return Response("ok")
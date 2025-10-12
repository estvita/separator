import re
import redis
import logging
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from separator.waweb.models import Session
from rest_framework import permissions

from django.utils import timezone
import separator.waweb.tasks as tasks
import separator.bitrix.tasks as bitrix_tasks
from separator.waweb.tasks import event_processor

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

logger = logging.getLogger("django")


class EventsHandler(GenericViewSet):
    def create(self, request, *args, **kwargs):
        event_data = request.data
        event_processor.delay(event_data)            
        return Response("ok")
    
    
    @action(detail=False, methods=['post'], url_path=r'(?P<session>[^/.]+)/send', permission_classes=[permissions.AllowAny])
    def send(self, request, session=None, *args, **kwargs):
        session_id = session

        if not session_id:
            return Response({'error': 'session is required'})

        try:
            session = Session.objects.get(session=session_id)
        except Exception as e:
            return Response({'error': 'An error occurred', 'details': str(e)})
        
        if session.date_end and timezone.now() > session.date_end:
            return Response({'error': 'tariff has expired'}, status=402)

        data = request.data
        event = data.get('event')       
        message_type = data.get('message_type')
        attachments = data.get('attachments', {})

        if event == "message_created" and message_type == "outgoing":
            message_id = data.get('id')

            if redis_client.exists(f'chatwoot:{message_id}'):
                return Response({'message': 'loop message'})
        
            content = data.get('content')
            conversation = data.get('conversation', {})
            meta = conversation.get('meta', {})
            sender = meta.get('sender', {})
            phone_number = sender.get('phone_number')

            if content:
                tasks.send_message.delay(session_id, phone_number, content)

                # Если подключен битрикс
                if session.line:
                    cleaned_phone = re.sub(r'\D', '', phone_number)
                    bitrix_tasks.message_add.delay(session.app_instance.id, session.line.line_id, 
                                                   cleaned_phone, content, session.line.connector.code)
            
            if attachments:
                for attachment in attachments:
                    tasks.send_message_task.delay(str(session.session), [phone_number], attachment, 'media')
                return Response({'message': 'All files sent successfully'})

        return Response({'message': f'Session {session_id} authorized'})
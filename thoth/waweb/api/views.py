from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from thoth.waweb.models import Session
from rest_framework import permissions
import requests

from django.conf import settings
import redis
import logging
import uuid
import re
from django.utils import timezone

import thoth.chatwoot.utils as chatwoot
from thoth.chatwoot.tasks import new_inbox
import thoth.waweb.tasks as tasks

import thoth.bitrix.utils as bitrix_utils
import thoth.bitrix.tasks as bitrix_tasks


redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

logger = logging.getLogger("django")


class EventsHandler(GenericViewSet):
    def create(self, request, *args, **kwargs):
        event_data = request.data
        sessionid = event_data.get('instance')
        if not sessionid:
            return Response({'error': 'sessionId is required'})
        
        try:
            uuid_obj = uuid.UUID(sessionid)
        except ValueError:
            print("Invalid UUID format for sessionId")
            return Response({'error': 'Invalid UUID format for sessionId'})

        try:
            session = Session.objects.get(session=sessionid)
        except Session.DoesNotExist:
            return Response({'error': f'Session with sessionId {sessionid} does not exist'})
        
        if not session.owner:
            return Response({'error': 'Session has no owner'})
        
        event = event_data.get("event")
        data = event_data.get("data", {})
        apikey = event_data.get('apikey')
        if apikey and session.apikey != apikey:
            session.apikey = apikey
            session.save(update_fields=["apikey"])

        server = session.server
        headers = {"apikey": session.apikey}
        
        if event == "connection.update":
            state = data.get('state')
            if session.status != state:
                session.status = state
                session.save(update_fields=["status"])

            if state == "open":
                wuid = data.get("wuid")
                number = wuid.split("@")[0]
                session.phone = number
                session.save(update_fields=["phone"])

                if Session.objects.exclude(pk=session.pk).filter(phone=number).exists():
                    headers = {"apikey": server.api_key}
                    response = requests.delete(f"{server.url}instance/logout/{sessionid}", headers=headers)
                    response = requests.delete(f"{server.url}instance/delete/{sessionid}", headers=headers)
                    session.delete()
                    return Response({'error': 'Phone number already in use, session deleted'})
                
                # создание Inbox в чатвут
                if settings.CHATWOOT_ENABLED and not session.inbox:
                    new_inbox.delay(sessionid, number)
                    return Response({'event processed.'})                

        elif event in ["messages.upsert", "send.message"]:
            if session.date_end and timezone.now() > session.date_end:
                return Response({'error': 'tariff has expired'})
            
            message = data.get('message', {})
            key_data = data.get('key', {})
            message_id = key_data.get('id')

            if redis_client.exists(f'waweb:{message_id}'):
                return Response({'message': 'loop message'})

            fromme = key_data.get('fromMe')
            sender = event_data.get('sender')
            if sender:
                sender = sender.split('@')[0]
            else:
                return Response({'sender not found'})
            remoteJid = key_data.get('remoteJid')
            pushName = data.get("pushName")
            group_message = False
            # если g.us значит группа
            if "g.us" in remoteJid:
                participantPn = key_data.get('participantPn')
                participant = key_data.get('participant')
                group_message = True
                if participantPn and "@lid" not in participantPn:
                    participant = participantPn
                if "@lid" in participant:
                    try:
                        participants_data = requests.get(f"{server.url}group/participants/{sessionid}", 
                                                    params={"groupJid": remoteJid}, headers=headers)
                        participants_data.raise_for_status()
                        participants_dict = participants_data.json()
                        participants_list = participants_dict.get('participants', [])
                        participant = next((p['jid'] for p in participants_list if p['lid'] == participant), None)
                    except Exception:
                        print(participants_data.json())
                        pass
                participant = participant.split('@')[0]
                participant = f"{pushName} ({participant})"
                params = {"groupJid": remoteJid}
                group_name = requests.get(f"{server.url}group/findGroupInfos/{sessionid}", params=params, headers=headers)
                if group_name.status_code == 200:
                    pushName = group_name.json().get("subject")
            file_data = {}
            remoteJid = remoteJid.split('@')[0]

            profilepic_url = None
            if not group_message:
                profilepic = requests.post(f"{server.url}chat/fetchProfilePictureUrl/{sessionid}", 
                                        json={"number": remoteJid}, headers=headers)
                if profilepic.status_code == 200:
                    profilepic = profilepic.json()
                    profilepic_url = profilepic.get("profilePictureUrl")
            
            payload = {
                'sender': sender,
                'remoteJid': remoteJid,
                'fromme': fromme,
            }

            msg_type = data.get('messageType')
            fileName = None

            if msg_type == 'conversation':
                payload.update({'content': message.get('conversation')})

            elif msg_type == 'locationMessage':
                location = message.get(msg_type, {})
                latitude  = location.get('degreesLatitude')
                longitude  = location.get('degreesLongitude')
                description = f"{location.get('name')}: {location.get('address')}"
                body = f"Link: https://www.google.com/maps/place/{latitude},{longitude}"
                if "None" not in description:
                    body = f"Address: {description} \n {body}"
                payload.update({'content': body})

            elif msg_type == 'contactMessage':
                payload.update({'content': message.get(msg_type, {}).get("vcard")})
            
            elif msg_type == 'interactiveResponseMessage':
                interactive = message.get(msg_type, {})
                body = interactive.get("body", {})
                text = body.get("text")
                response = interactive.get("nativeFlowResponseMessage", {})
                name = response.get("name", "")
                payload['content'] = f"{name} {text}"

            elif msg_type == 'templateMessage':
                template = message.get('templateMessage', {})
                content = title = footer = None
                hydrated = template.get('hydratedTemplate')
                if hydrated:
                    title = hydrated.get("hydratedTitleText", "")
                    content = hydrated.get("hydratedContentText", "")
                    footer = hydrated.get("hydratedFooterText", "")
                interactive = template.get('interactiveMessageTemplate')
                if interactive:
                    title = (interactive.get('header') or {}).get('title', title or '')
                    content = (interactive.get('body') or {}).get('text', content or '')
                payload['content'] = f"{(title or '').strip()} \n {(content or '').strip()} \n {(footer or '').strip()}"

            elif msg_type in ["imageMessage", "documentMessage", "videoMessage", "audioMessage"]:
                payload.update({'content': message.get(msg_type, {}).get("caption")})
                media_url = f"{server.url}chat/getBase64FromMediaMessage/{sessionid}"
                msg_payload = {"message": {"key": {"id": message_id}}}
                response = requests.post(media_url, json=msg_payload, headers=headers)
                if response.status_code == 201:
                    file_data = response.json()
                    file_body = file_data.get('base64')
                    fileName = file_data.get('fileName')
                    mimetype = file_data.get('mimetype')
                    if file_body:
                        from io import BytesIO
                        import base64
                        file_bytes = base64.b64decode(file_body)
                        file_like = BytesIO(file_bytes)
                        file_like.name = fileName
                        payload.update({'attachments': (file_like.name, file_like, mimetype)})
            else:
                return Response({'message': 'ok'})
            
            try:
                # chatwoot не поддерживает группы, поэтому фильтруем
                if settings.CHATWOOT_ENABLED and session.inbox and not group_message:
                    try:
                        resp_chatwoot = chatwoot.send_api_message(session.inbox, payload)
                        if resp_chatwoot.status_code == 200:
                            cw_msg_id = resp_chatwoot.json().get("id")
                            redis_client.setex(f'chatwoot:{cw_msg_id}', 600, cw_msg_id)
                    except Exception:
                        pass
                
                # отправка сообщения в битрикс
                if session.line:
                    line = session.line
                    download_url = None
                    text = payload.get("content", None)
                    if file_data:
                        member_id = line.portal.member_id
                        chat_key = f'bitrix_chat:{member_id}:{line.line_id}:{remoteJid}'
                        if redis_client.exists(chat_key):
                            upload_file = bitrix_utils.upload_file(
                                session.app_instance, session.app_instance.storage_id,
                                file_body, fileName)
                            if upload_file:
                                download_url = upload_file.get("DOWNLOAD_URL")

                    if group_message and text:
                        text = f"{participant}: {text}"
                    attach = None
                    if fromme:
                        file_url = None
                        file_id = upload_file.get("ID", None) if download_url else None
                        if file_id:
                            file_link = bitrix_utils.call_method(session.app_instance, "disk.file.getExternalLink", {"id": file_id})
                            if "result" in file_link:
                                file_url = file_link.get("result")
                        source = data.get("source", "")
                        from_app = f"[B]Отправлено из WhatsApp {source}[/B][BR]"
                        if file_url:
                            file_name = fileName[-fileName[::-1].find('.')-5:]
                            text = f"{from_app} [BR] {text}" if text else from_app
                            attach = [
                                {
                                    "FILE": {
                                        "NAME": file_name,
                                        "LINK": file_url
                                    }
                                }
                            ]
                        else:
                            text = f"{from_app} {text}"
                        bitrix_tasks.message_add.delay(session.app_instance.id, line.line_id, 
                                                    remoteJid, text, line.connector.code, attach)

                    else:
                        if download_url:
                            attach = [
                                {
                                    "url": download_url,
                                    "name": fileName
                                }
                            ]
                        bitrix_tasks.send_messages.delay(session.app_instance.id, remoteJid, text, line.connector.code, line.line_id,
                                                            False, pushName, message_id, attach, profilepic_url)
                        
            except Exception as e:
                import traceback
                print(traceback.format_exc())
                return Response({'error': f'Failed to send API message: {str(e)}'}, status=500)
            
        return Response({'message': 'ok'})
    
    
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
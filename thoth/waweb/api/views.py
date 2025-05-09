from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from thoth.waweb.models import WaSession, WaServer
from rest_framework import permissions
import requests
from django.contrib.sites.models import Site
from django.conf import settings
import redis
import uuid
import re
from django.utils import timezone

import thoth.chatwoot.utils as chatwoot
from thoth.chatwoot.models import Inbox
import thoth.waweb.tasks as tasks

import thoth.waweb.utils as utils
import thoth.bitrix.utils as bitrix_utils
import thoth.bitrix.tasks as bitrix_tasks


SITE_ID = settings.SITE_ID
WABWEB_SRV = settings.WABWEB_SRV

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


class WaEventsHandler(GenericViewSet):
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
            session = WaSession.objects.get(session=sessionid)
        except WaSession.DoesNotExist:
            return Response({'error': f'WaSession with sessionId {sessionid} does not exist'})
        
        event = event_data.get("event")
        data = event_data.get("data", {})
        apikey = event_data.get('apikey')
        if apikey and session.apikey != apikey:
            session.apikey = apikey
            session.save()

        wa_server = WaServer.objects.get(id=WABWEB_SRV)
        headers = {"apikey": session.apikey}
        
        if event == "connection.update":
            state = data.get('state')
            if session.status != state:
                session.status = state
                session.save()
            if state == "open":
                if not session.phone:
                    wuid = data.get("wuid")
                    number = wuid.split("@")[0]
                    if WaSession.objects.filter(phone=number).exists():
                        response = requests.delete(
                            f"{wa_server.url}instance/logout/{sessionid}",
                            headers={"apikey": wa_server.api_key},
                        )
                        response = requests.delete(
                            f"{wa_server.url}instance/delete/{sessionid}",
                            headers={"apikey": wa_server.api_key},
                        )
                        session.delete()
                        return Response({'error': 'Phone number already in use, session deleted'})
                    session.phone = number
                    session.save()
                
                # создание Inbox в чатвут
                if not settings.CHATWOOT_ENABLED:
                    return Response({'message': 'event processed. close'})
                
                if not session.inbox:
                    site = Site.objects.get(id=SITE_ID)

                    inbox_data = {
                        'name': number,
                        'lock_to_single_conversation': True,
                        'channel': {
                            'type': 'api',
                            'webhook_url': f'https://{site.domain}/api/waweb/{sessionid}/send/'
                        }
                    }
                    resp = chatwoot.add_inbox(session.owner, inbox_data)
                    if "result" in resp:
                        result = resp.get('result', {})
                        try:
                            inbox, created = Inbox.objects.update_or_create(
                                owner=session.owner,
                                id=result.get('inbox_id'),
                                defaults={
                                    'account': result.get('account'),
                                }
                            )
                            session.inbox = inbox
                            session.save()
                        except Exception as e:
                            print('EROOR!!', e)
                            return Response({'error': f'Failed to create/update Inbox: {str(e)}'}, status=500)


        elif event in ["messages.upsert", "send.message"]:
            if timezone.now() > session.date_end:
                return Response({'error': 'tariff has expired'})
            
            message = data.get('message', {})
            key_data = data.get('key', {})
            message_id = key_data.get('id')

            if redis_client.exists(f'waweb:{message_id}'):
                return Response({'message': 'loop message'})

            fromme = key_data.get('fromMe')
            sender = event_data.get('sender').split('@')[0]
            remoteJid = key_data.get('remoteJid')
            # participant - этот тот, кто отправил сообщение в группе ватсапа
            participant = key_data.get('participant')
            pushName = data.get("pushName")
            group_message = False
            # если есть participant значит группа
            if participant:
                group_message = True
                params = {"groupJid": remoteJid}
                group_name = requests.get(f"{wa_server.url}group/findGroupInfos/{sessionid}", params=params, headers=headers)
                if group_name.status_code == 200:
                    pushName = group_name.json().get("subject")
            file_data = {}
            remoteJid = remoteJid.split('@')[0]

            profilepic_url = None
            if not group_message:
                profilepic = requests.post(f"{wa_server.url}chat/fetchProfilePictureUrl/{sessionid}", 
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

            elif msg_type == 'templateMessage':
                hydratedTemplate = message.get(msg_type, {}).get("hydratedTemplate", {})
                hydratedTitleText = hydratedTemplate.get("hydratedTitleText")
                hydratedContentText = hydratedTemplate.get("hydratedContentText")
                hydratedFooterText = hydratedTemplate.get("hydratedFooterText")
                payload.update({'content': f"{hydratedTitleText} \n {hydratedContentText} \n {hydratedFooterText}"})

            elif msg_type in ["imageMessage", "documentMessage", "videoMessage", "audioMessage"]:
                payload.update({'content': message.get(msg_type, {}).get("caption")})
                media_url = f"{wa_server.url}chat/getBase64FromMediaMessage/{sessionid}"
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
                if not group_message and settings.CHATWOOT_ENABLED:
                    resp_chatwoot = chatwoot.send_api_message(session.inbox, payload)
                    if resp_chatwoot.status_code == 200:
                        cw_msg_id = resp_chatwoot.json().get("id")
                        redis_client.setex(f'chatwoot:{cw_msg_id}', 600, cw_msg_id)
                
                # отправка сообщения в битрикс
                if session.line:
                    file_url = None
                    attach = None
                    text = payload.get("content") or fileName
                    if file_data:
                        domain = session.line.portal.domain
                        chat_key = f'bitrix_chat:{domain}:{session.line.line_id}:{remoteJid}'
                        if redis_client.exists(chat_key):
                            chat_id = redis_client.get(chat_key).decode('utf-8')
                            chat_folder = bitrix_utils.call_method(session.app_instance, "im.disk.folder.get", {"CHAT_ID": chat_id})
                            if "result" in chat_folder:
                                bitrix_utils.call_method(session.app_instance, "imopenlines.session.join", {"CHAT_ID": chat_id})
                                folder_id =  chat_folder.get("result").get("ID")
                                upload_file = bitrix_utils.upload_file(
                                    session.app_instance, folder_id,
                                    file_body, fileName)
                                if upload_file:
                                    file_url = upload_file.get("DOWNLOAD_URL")
                    if fromme:
                        bitrix_tasks.message_add.delay(session.app_instance.id, session.line.line_id, 
                                                       remoteJid, text, session.line.connector.code)
                        if file_url:
                            file_upd = {
                                "CHAT_ID": chat_id,
                                "UPLOAD_ID": upload_file.get("FILE_ID"),
                                "DISK_ID": upload_file.get("ID"),
                                "SILENT_MODE": "Y",
                                "MESSAGE": fileName
                            }
                            bitrix_tasks.call_api.delay(session.app_instance.id, "im.disk.file.commit", file_upd)
                    else:
                        attach = None
                        if file_url:
                            attach = [
                                {
                                    "url": file_url,
                                    "name": fileName
                                }
                            ]
                        bitrix_tasks.send_messages.delay(session.app_instance.id, remoteJid, text, session.line.connector.code, session.line.line_id,
                                                            False, pushName, message_id, attach, profilepic_url)
                        

                return Response({"message": "message processed"})

            except Exception as e:
                print(f'Failed to send API message: {str(e)}')
                return Response({'error': f'Failed to send API message: {str(e)}'}, status=500)
            
        return Response({'message': 'ok'})
    
    
    @action(detail=False, methods=['post'], url_path=r'(?P<session>[^/.]+)/send', permission_classes=[permissions.AllowAny])
    def send(self, request, session=None, *args, **kwargs):
        session_id = session

        # Проверяем, передан ли session
        if not session_id:
            return Response({'error': 'session is required'})

        try:
            # Проверяем наличие сессии в базе
            session = WaSession.objects.get(session=session_id)
        except Exception as e:
            return Response({'error': 'An error occurred', 'details': str(e)})
        
        if timezone.now() > session.date_end:
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
                wa_resp = utils.send_message(session_id, phone_number, content)
                if wa_resp.status_code == 201:
                    utils.store_msg(wa_resp)

                # Если подключен битрикс
                if session.line:
                    cleaned_phone = re.sub(r'\D', '', phone_number)
                    bitrix_tasks.message_add.delay(session.app_instance.id, session.line.line_id, 
                                                   cleaned_phone, content, session.line.connector.code)
            
            if attachments:
                for attachment in attachments:
                    tasks.send_message_task.delay(str(session.session), [phone_number], attachment, 'media')
                return Response({'message': 'All files sent successfully'})

        # Если сессия найдена, запрос авторизован
        return Response({'message': f'Session {session_id} authorized'})
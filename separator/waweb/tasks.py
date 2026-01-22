import re
import uuid
import redis
import requests
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from celery import shared_task
from django.utils.translation import gettext as _
import separator.waweb.utils as utils
from separator.waweb.models import Session

from django.conf import settings
import separator.bitrix.utils as bitrix_utils
import separator.bitrix.tasks as bitrix_tasks

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)

@shared_task(queue='waweb')
def send_message(session_id, recipient, content, cont_type="string"):
    try:
        session = Session.objects.get(session=session_id)
        if session.date_end and timezone.now() > session.date_end:
            raise Exception({'tariff has expired'})
        server = session.server
        headers = {"apikey": session.apikey}
        cleaned = re.sub(r'\D', '', recipient)
        if cont_type == "string":
            payload = {
                "number": cleaned,
                "text": content,
                "linkPreview": True,
            }
            url = f"{server.url}/message/sendText/{session_id}"
            resp = requests.post(url, json=payload, headers=headers)
        elif cont_type == "media":
            content = utils.download_file(content)
            url = f"{server.url}/message/sendMedia/{session_id}"
            mimetype = content.get("mimetype", "")
            base_type = mimetype.split('/')[0]
            mediatype = base_type if base_type in ["image"] else "document"
            payload = {
                "number": cleaned,
                "mediatype": mediatype,
                "mimetype": content.get("mimetype"),
                "media": content.get("data"),
                "fileName": content.get("filename")
            }
            resp = requests.post(url, json=payload, headers=headers)
        else:
            raise Exception("Unknown cont_type")

        if resp and resp.status_code == 201:
            utils.store_msg(resp)
            return resp
        else:
            raise Exception(f"Request failed: {resp.status_code}, {resp.text}")

    except Exception as e:
        raise


@shared_task(queue='waweb')
def delete_sessions(days):
    now = timezone.now()
    filters = Q((Q(phone__isnull=True) | Q(phone='')) & Q(date_end__lt=now))
    if days is not None:
        try:
            days_int = int(days)
            date_limit = now - timedelta(days=days_int)
            filters = filters | Q(date_end__lt=date_limit)
        except (TypeError, ValueError):
            pass

    sessions = Session.objects.filter(filters)
    for session in sessions:
        server = session.server
        headers = {"apikey": server.api_key}
        url = f"{server.url}/instance/delete/{session.session}"
        requests.delete(url, headers=headers)

@shared_task(queue='waweb')
def event_processor(event_data):
    sessionid = event_data.get('instance')
    if not sessionid:
        raise Exception({'sessionId is required'})
    
    try:
        uuid_obj = uuid.UUID(sessionid)
    except ValueError:
        raise Exception({'Invalid UUID format for sessionId'})

    try:
        session = Session.objects.get(session=sessionid)
    except Session.DoesNotExist:
        raise Exception({f'Session with sessionId {sessionid} does not exist'})
    
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
            check_sessions = Session.objects.filter(phone=number).exclude(session=sessionid)
            for other_session in check_sessions:
                other_session.phone = None
                other_session.save(update_fields=['phone'])
                if other_session.server:
                    headers = {"apikey": other_session.server.api_key}
                    url = f"{other_session.server.url.rstrip('/')}/instance/delete/{other_session.session}"
                    requests.delete(url, headers=headers)

            if not session.date_end and "separator.tariff" in settings.INSTALLED_APPS:
                from separator.tariff.utils import get_trial
                session.date_end = get_trial(session.owner, "waweb")
            
            # create lead in b24
            if session.phone != number and not session.owner.integrator:
                from separator.bitrix.tasks import prepare_lead
                prepare_lead.delay(session.owner.id, f'New WhatsApp Web: {number}')            
            
            session.phone = number
            session.save()

    elif event in ["messages.upsert", "send.message"]:
        if session.date_end and timezone.now() > session.date_end:
            raise Exception({'tariff has expired'})
        
        message = data.get('message', {})
        key_data = data.get('key', {})
        message_id = key_data.get('id')

        if redis_client.exists(f'waweb:{message_id}'):
            raise Exception({'loop message'})

        fromme = key_data.get('fromMe')
        sender = event_data.get('sender')
        if sender:
            sender = sender.split('@')[0]
        else:
            raise Exception({'sender not found'})
        if sender:
            sender = sender.split('@')[0]
        else:
            raise Exception({'sender not found'})
        
        # Определение remote_user (ID чата)
        addressingMode = key_data.get('addressingMode')
        remote_jid_raw = key_data.get('remoteJid')
        
        # Если это группа, всегда берем remoteJid, так как remoteJidAlt может быть пустым
        if remote_jid_raw and "g.us" in remote_jid_raw:
            remote_user = remote_jid_raw
        # Для личных чатов используем логику addressingMode
        elif addressingMode == "lid":
            remote_user = key_data.get('remoteJidAlt')
        else:
            remote_user = remote_jid_raw

        if not remote_user:
             # Fallback если вдруг remoteJidAlt пустой при lid (хотя не должно быть в личке)
             remote_user = remote_jid_raw

        pushName = data.get("pushName")
        if pushName:
            pushName = re.sub(r'[^\w\s\-\']', '', pushName).strip()

        group_message = False
        
        # если g.us значит группа
        if "g.us" in remote_user:
            participantPn = key_data.get('participantPn')
            participant = key_data.get('participant')
            group_message = True
            
            # Пытаемся найти номер телефона участника
            if participantPn and "@lid" not in participantPn:
                participant = participantPn
            
            # Если у нас все еще LID, пробуем разрешить его через API
            if participant and "@lid" in participant:
                try:
                    # Нормализуем искомый LID (убираем суффикс для сравнения)
                    target_lid = participant.split('@')[0]
                    
                    participants_data = requests.get(f"{server.url}/group/participants/{sessionid}", 
                                                params={"groupJid": remote_user}, headers=headers)
                    participants_data.raise_for_status()
                    participants_dict = participants_data.json()
                    participants_list = participants_dict.get('participants', [])
                    
                    # Ищем участника, сравнивая LID без суффиксов
                    found_participant = next((
                        p for p in participants_list 
                        if p.get('lid', '').split('@')[0] == target_lid
                    ), None)
                    
                    if found_participant:
                        participant = found_participant.get('id') # Берем JID (номер телефона)
                        
                except Exception as e:
                    print(f"Error resolving participant: {e}")
                    pass
            
            participant = participant.split('@')[0] if participant else "Unknown"
            participant = f"{pushName} ({participant})" if pushName else participant
            
            params = {"groupJid": remote_user}
            group_name = requests.get(f"{server.url}/group/findGroupInfos/{sessionid}", params=params, headers=headers)
            if group_name.status_code == 200:
                pushName = group_name.json().get("subject")
        file_data = {}
        remote_user = remote_user.split('@')[0]

        profilepic_url = None
        if not group_message:
            profilepic = requests.post(f"{server.url}/chat/fetchProfilePictureUrl/{sessionid}", 
                                    json={"number": remote_user}, headers=headers)
            if profilepic.status_code == 200:
                profilepic = profilepic.json()
                profilepic_url = profilepic.get("profilePictureUrl")

        msg_type = data.get('messageType')
        fileName = None

        if msg_type == 'conversation':
            text = message.get('conversation')

        elif msg_type == 'locationMessage':
            location = message.get(msg_type, {})
            latitude  = location.get('degreesLatitude')
            longitude  = location.get('degreesLongitude')
            description = f"{location.get('name')}: {location.get('address')}"
            body = f"Link: https://www.google.com/maps/place/{latitude},{longitude}"
            if "None" not in description:
                body = f"Address: {description} \n {body}"
            text = body

        elif msg_type == 'contactMessage':
            text = message.get(msg_type, {}).get("vcard")
        
        elif msg_type == 'interactiveResponseMessage':
            interactive = message.get(msg_type, {})
            body = interactive.get("body", {})
            text = body.get("text")
            response = interactive.get("nativeFlowResponseMessage", {})
            name = response.get("name", "")
            text = f"{name} {text}"

        elif msg_type == 'reactionMessage':
            reaction = message.get(msg_type, {})
            text = reaction.get("text")

        elif msg_type == 'ephemeralMessage':
            ephemeralMessage = message.get(msg_type, {})
            extendedTextMessage = (
                ephemeralMessage.get('message', {})
                .get('extendedTextMessage', {})
            )
            text = extendedTextMessage.get('text')

        elif msg_type == 'templateMessage':
            template = message.get(msg_type, {})
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
            text = f"{(title or '').strip()} \n {(content or '').strip()} \n {(footer or '').strip()}"

        elif msg_type in ["imageMessage", "documentMessage", "videoMessage", "audioMessage"]:
            text = message.get(msg_type, {}).get("caption")
            media_url = f"{server.url}/chat/getBase64FromMediaMessage/{sessionid}"
            msg_payload = {"message": {"key": {"id": message_id}}}
            response = requests.post(media_url, json=msg_payload, headers=headers)
            if response.status_code == 201:
                file_data = response.json()
                file_body = file_data.get('base64')
                fileName = file_data.get('fileName')
                if file_body:
                    from io import BytesIO
                    import base64
                    file_bytes = base64.b64decode(file_body)
                    file_like = BytesIO(file_bytes)
                    file_like.name = fileName
        
        try:
            
            # отправка сообщения в битрикс
            if session.line:
                line = session.line
                # Prepare file if present
                file_content_bytes = None
                if file_data and redis_client.exists(f'bitrix_chat:{line.portal.member_id}:{line.line_id}:{remote_user}'):
                    import base64
                    file_content_bytes = base64.b64decode(file_body)

                if group_message and text:
                    text = f"{participant}: {text}"
                attach = None
                
                if fromme:
                    # Echo message (system message) -> needs permanent storage in Bitrix
                    file_url = None
                    if file_content_bytes:
                         file_url = bitrix_utils.upload_and_get_link(
                             session.app_instance, file_content_bytes, fileName
                         )
                    
                    source = data.get("source", "")
                    if source in (None, "unknown"):
                        source = ""
                    from_app = f"[B]{_('From WhatsApp')} {source}[/B][BR]"
                    
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
                    if text and not attach:
                        text = f"{from_app} {text}"
                    if text or attach:
                        bitrix_tasks.message_add.delay(session.app_instance.id, line.line_id, 
                                                    remote_user, text, line.connector.code, attach)

                else:
                    # Incoming message -> use temporary link for connector ingestion
                    download_url = None
                    if file_content_bytes:
                         download_url = bitrix_utils.save_temp_file(
                             file_content_bytes, fileName, session.app_instance
                         )

                    if download_url:
                        attach = [
                            {
                                "url": download_url,
                                "name": fileName
                            }
                        ]
                    bitrix_tasks.send_messages.delay(session.app_instance.id, remote_user, text, line.connector.code, line.line_id,
                                                        False, pushName, message_id, attach, profilepic_url)
                    
        except Exception as e:
            raise Exception(event_data)
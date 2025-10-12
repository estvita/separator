import re
import uuid
import redis
import requests
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from celery import shared_task
import separator.waweb.utils as utils
from separator.waweb.models import Session

from django.conf import settings
import separator.chatwoot.utils as chatwoot
from separator.chatwoot.tasks import new_inbox
import separator.bitrix.utils as bitrix_utils
import separator.bitrix.tasks as bitrix_tasks

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

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
            url = f"{server.url}message/sendText/{session_id}"
            resp = requests.post(url, json=payload, headers=headers)
        elif cont_type == "media":
            url = f"{server.url}message/sendMedia/{session_id}"
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
            return resp.json()
        else:
            raise Exception(f"Request failed: {resp.status_code}, {resp.text}")

    except Exception as e:
        raise


@shared_task(queue='waweb')
def send_message_task(session_id, recipients, content, cont_type="string"):
    if cont_type == "media":
        content = utils.download_file(content)
    for recipient in recipients:
        send_message.delay(session_id, recipient, content, cont_type)


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
        url = f"{server.url}instance/delete/{session.session}"
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
            session.phone = number

            if not session.date_end and "separator.tariff" in settings.INSTALLED_APPS:
                from separator.tariff.utils import get_trial
                session.date_end = get_trial(session.owner, "waweb")
            session.save()

            # создание Inbox в чатвут
            if settings.CHATWOOT_ENABLED and not session.inbox:
                new_inbox.delay(sessionid, number)

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

        elif msg_type == 'reactionMessage':
            reaction = message.get(msg_type, {})
            payload['content'] = reaction.get("text")

        elif msg_type == 'ephemeralMessage':
            ephemeralMessage = message.get(msg_type, {})
            extendedTextMessage = (
                ephemeralMessage.get('message', {})
                .get('extendedTextMessage', {})
            )
            payload['content'] = extendedTextMessage.get('text')

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
                        file_link = bitrix_tasks.call_api(session.app_instance.id, "disk.file.getExternalLink", {"id": file_id})
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
                        if text:
                            text = f"{from_app} {text}"
                        else:
                            raise Exception(event_data)
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
            raise
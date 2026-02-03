import json
import hmac
import redis
import logging
import hashlib
import re
from datetime import datetime

import requests
from django.db import OperationalError
from django.utils import timezone
from django.utils.translation import gettext as _
from django.conf import settings
from celery import shared_task

from separator.bitrix.models import Line
import separator.bitrix.tasks as bitrix_tasks
import separator.bitrix.utils as bitrix_utils

from .models import App, Phone, Waba, Template, Event, Error

API_URL = settings.FACEBOOK_API_URL

logger = logging.getLogger("django")
# Add timeouts to prevent hanging if Redis is unavailable
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


def call_api(app: App=None, waba: Waba=None, endpoint: str=None, method="get", payload=None, file_url=None, files=None, data=None):
    if not app and waba:
        access_token = waba.access_token
        app = waba.app
    else:
        access_token = app.access_token
    headers = {"Authorization": f"Bearer {access_token}"}
    base_url = f"{API_URL}/v{app.api_version}.0"

    resp = None
    try:
        if file_url:
            return requests.get(file_url, headers=headers)
        if method == "get":
            resp = requests.get(f"{base_url}/{endpoint}", params=payload, headers=headers)
        elif method == "post":
            if files:
                resp = requests.post(f"{base_url}/{endpoint}", data=data, files=files, headers=headers)
            else:
                resp = requests.post(f"{base_url}/{endpoint}", json=payload, headers=headers)

        resp_data = resp.json()
        if "error" in resp_data:
            raise Exception(resp_data)

        return resp_data
    except Exception:
        raise


def upload_media(appinstance, file_content, mime_type, filename, line_id=None, phone_num=None):
    phone = None
    if phone_num:
        phone = Phone.objects.filter(phone=f"+{phone_num}").first()
        waba = phone.waba
    elif line_id:
        line = Line.objects.filter(line_id=line_id, app_instance=appinstance).first()
        waba = Waba.objects.filter(phones__line=line).first() if line else None
        phone = waba.phones.filter(line=line).first() if waba and line else None
    else:
        return {"error": True, "message": "phone not found"}

    if not phone or not waba:
        return {"error": True, "message": "not phone or not waba"}

    if phone.date_end and timezone.now() > phone.date_end:
        return {"error": True, "message": "phone tariff expired"}

    if not phone.phone_id:
        return {"error": True, "message": "not phone phone_id"}

    try:
        files = {
            'file': (filename, file_content, mime_type)
        }
        data = {
            'messaging_product': 'whatsapp'
        }
        return call_api(waba=waba, endpoint=f"{phone.phone_id}/media", method="post", files=files, data=data)
    except Exception as e:
        return {"error": True, "message": str(e)}


def send_message(appinstance, message, line_id=None, phone_num=None):
    phone = None
    if phone_num:
        phone = Phone.objects.filter(phone=f"+{phone_num}").first()
        waba = phone.waba
    elif line_id:
        line = Line.objects.filter(line_id=line_id, app_instance=appinstance).first()
        waba = Waba.objects.filter(phones__line=line).first() if line else None
        phone = waba.phones.filter(line=line).first() if waba and line else None
    else:
        return {"error": True, "message": "phone not found"}
    if not phone or not waba:
        return {"error": True, "message": "not phone or not waba"}
    if phone.date_end and timezone.now() > phone.date_end:
        return {"error": True, "message": "phone tariff expired"}
    if not phone.phone_id:
        return {"error": True, "message": "not phone phone_id"}
    try:
        return call_api(waba=waba, endpoint=f"{phone.phone_id}/messages", method="post", payload=message)
    except Exception as e:
        return {"error": True, "message": str(e)}


def get_file(media_url, filename, appinstance, waba):
    try:
        download_file = call_api(file_url=media_url, waba=waba)
    except Exception:
        return None
    
    # Use centralized logic for temp file handling
    file_url = bitrix_utils.save_temp_file(download_file.content, filename, appinstance)
    return file_url


def format_contacts(contacts):
    contact_text = _("Присланы контакты:\n")
    for i, contact in enumerate(contacts, start=1):
        name = contact["name"]["formatted_name"]
        phones = ", ".join([phone["phone"] for phone in contact.get("phones", [])])
        emails = ", ".join([email["email"] for email in contact.get("emails", [])])

        contact_info = f"{i}. {name}"
        if phones:
            contact_info += f", {phones}"
        if emails:
            contact_info += f", {emails}"

        contact_text += contact_info + "\n"

    return contact_text


def message_template_status_update(entry):
    waba_id = entry.get('id')
    changes = entry.get('changes', [])[0]
    value = changes.get('value', {})
    event = value.get('event')
    template_id = value.get('message_template_id')
    template_name = value.get('message_template_name')
    lang = value.get('message_template_language')
    if event == "APPROVED":
        waba = Waba.objects.filter(waba_id=waba_id).first()
        if waba:
            components = None
            try:
                temp_data = call_api(waba=waba, endpoint=template_id)
                components = temp_data.get('components')
            except Exception:
                pass

            template, created = Template.objects.update_or_create(
                owner=waba.owner,
                id=template_id,
                defaults={
                    'waba': waba,
                    'name': template_name,
                    'lang': lang,
                    'content': components,
                    'status': event
                }
            )

    elif event == 'PENDING_DELETION':
        try:
            template = Template.objects.filter(id=template_id, waba__waba_id=waba_id).first()
            if template:
                template.delete()
        except Template.DoesNotExist:
            raise Exception(f"Template not found: {entry}")

    else:
        raise Exception(entry)
    

def error_message(data):
    error = (data.get('errors') or [{}])[0]
    code = error.get("code")
    fb_message = error.get("message") or error.get("title") or ""
    fb_details = (error.get("error_data") or {}).get("details", "")

    try:
        error_obj, created = Error.objects.get_or_create(
            code=code,
            defaults={"message": fb_message, "details": fb_details}
        )

        if error_obj.original:
            return str(data)
        
        out_message = f"Error for: {data.get('recipient_id')}:\n" \
                    f"{error_obj.message or fb_message}\n" \
                    f"{error_obj.details or fb_details}"
        return out_message
    except Exception:
        return f"Error for: {data.get('recipient_id')}: {fb_message} {fb_details}"


def extract_waba_id(data):
    entry = data.get("entry", [{}])[0]
    waba_id = entry.get("id")
    changes = entry.get("changes", [{}])
    if changes:
        value = changes[0].get("value", {})
        if "waba_info" in value:
            waba_id = value["waba_info"].get("waba_id", waba_id)
    return waba_id


@shared_task(
    queue='waba',
    autoretry_for=(OperationalError, requests.RequestException),
    default_retry_delay=5,
    max_retries=5
)
def event_processing(raw_body=None, signature=None, app_id=None, host=None):
    if signature and raw_body:
        if app_id:
            apps = App.objects.filter(client_id=app_id)
        elif host:
            domains = [host]
            if ':' in host:
                domains.append(host.split(':')[0])
            apps = App.objects.filter(site__domain__in=domains)
        else:
            apps = []

        verified = False
        payload = raw_body.encode('utf-8')
        for app in apps:
            if not app.client_secret:
                continue
            try:
                secret = app.client_secret.encode('utf-8')
                expected = 'sha256=' + hmac.new(secret, payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, expected):
                    verified = True
                    break
            except Exception as e:
                logger.error(f"Error verifying signature for app {app.id}: {e}")
        
        if not verified:
            logger.warning(f"Signature verification failed. Signature: {signature}")
            raise Exception(f"Invalid signature: {signature}")

    if raw_body:
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON from raw_body")
            raise Exception("Invalid JSON")
    else:
        raise Exception("No data provided")

    waba_id = extract_waba_id(data)
    waba = Waba.objects.filter(waba_id=waba_id).first()
    try:
        if waba:
            if waba.app and waba.app.events:
                Event.objects.create(waba=waba, content=json.dumps(data, ensure_ascii=False))
        else:
            if settings.SAVE_UNBOUND_WABA_EVENTS:
                Event.objects.create(content=json.dumps(data, ensure_ascii=False))
    except Exception as e:
        pass

    entry = data["entry"][0]
    changes = entry["changes"][0]
    field = changes.get('field')
    value = changes.get('value', {})
    event = value.get('event')

    if field == 'account_update':
        if event == "PARTNER_APP_UNINSTALLED":
            try:
                if waba and settings.WABA_AUTO_DELETE_ENTITIES:
                    waba.delete()
            except Exception as e:
                logger.warning(f"Failed to delete Waba: {e}")
            return(data)
        elif event == "PHONE_NUMBER_REMOVED":
            if settings.WABA_AUTO_DELETE_ENTITIES:
                try:
                    phone_number = value.get("phone_number")
                    phone = Phone.objects.filter(phone=f"+{phone_number}", waba=waba).first()
                    if phone:
                        phone.delete()
                        return(f"Phone {phone_number} deleted")
                    else:
                        raise Exception(f"phone_number not found: {data}")
                except Exception:
                    raise Exception(data)
            return(data)
        else:
            raise Exception(data)
    
    metadata = value.get("metadata", {})
    if metadata:    
        phone_number = metadata.get('display_phone_number')
        phone_number_id = metadata.get('phone_number_id')
        try:
            phone = Phone.objects.get(phone_id=phone_number_id, waba=waba)
            appinstance = phone.app_instance
            appinstance.host = host
            if not appinstance:
                raise Exception(f"appinstance not connected: {data}")
        except Phone.DoesNotExist:
            raise Exception(f"phone_number not found: {data}")
        except Exception:
            raise Exception(data)

    if field == 'message_template_status_update':
        return message_template_status_update(entry)    

    elif field == 'phone_number_name_update':
        display_phone_number = value.get('display_phone_number')
        if display_phone_number and waba:
            
            phone = Phone.objects.filter(phone=f"+{display_phone_number}", waba=waba).first()
            
            if phone:
                pin = phone.pin
                payload = {
                    'messaging_product': 'whatsapp',
                    'pin': pin
                }
                try:
                    return call_api(waba=waba, endpoint=f"{phone.phone_id}/register", method="post", payload=payload)
                except Exception:
                    raise

    elif field == 'account_settings_update':
        value_type = value.get("type")
        if value_type == "phone_number_settings":
            phone_number_settings = value.get(value_type, {})
            phone_id = phone_number_settings.get('phone_number_id')
            calling = phone_number_settings.get('calling', {})
            if not calling:
                raise Exception(data)
            status = calling.get('status', '').lower()
            phone = Phone.objects.filter(phone_id=phone_id).first()
            if not phone:
                raise Exception(f"Phone with id {phone_id} not found")
            try:
                if status and phone.calling != status:
                    phone.calling = status
                    phone.save()
            except Exception:
                pass
        else:
            raise Exception(data)
    
    elif field == 'messages':
        if phone.date_end and timezone.now() > phone.date_end:
            raise Exception(f"phone tariff ended: {data}")

        if not phone.line or not phone.waba or not phone.app_instance:
            raise Exception(f"phone not connected to b24: {data}")

        messages = value.get("messages", [])
        filename = None
        file_url = None
        text = None
        user_name = None
        chat_url = None
        for message in messages:
            referral = message.get("referral")
            if referral:
                chat_url = referral.get("source_url")

            message_type = message.get("type")
            user_phone = message["from"]
            message_id = message["id"]
            contacts = value.get("contacts", [])
            if contacts:
                user_name = contacts[0].get("profile", {}).get("name")
                if user_name:
                    user_name = re.sub(r'[^\w\s\-\']', '', user_name).strip()

            if message_type == "text":
                text = message["text"]["body"]

            elif message_type in ["image", "video", "audio", "document"]:
                media_data = value["messages"][0][message_type]
                media_id = media_data["id"]
                media_url = media_data.get("url")
                extension = media_data["mime_type"].split("/")[1].split(";")[0]
                filename = f"wamid.{media_id}.{extension}"
                           
                caption = media_data.get("caption") or ""
                original_filename = media_data.get("filename")
                if original_filename:
                    caption = f"{original_filename} {caption}"
                caption = caption.strip() if caption else None
                
                # Store mapping media_id -> message_id in Redis (expire 3 months)
                try:
                    redis_client.set(f"wamid:{media_id}", message_id, ex=7776000)
                except Exception:
                    pass

                file_url = get_file(media_url, filename, appinstance, phone.waba)

            elif message_type == "contacts":
                contacts = value["messages"][0]["contacts"]
                text = format_contacts(contacts)

            elif message_type == "interactive":
                interactive = message.get("interactive", {})
                interactive_type = interactive.get("type")
                if interactive_type == "call_permission_reply":
                    reply = interactive.get("call_permission_reply", {})
                    responce = reply.get("response", "expiration_timestamp")
                    expiration = ""
                    expiration = reply.get("expiration_timestamp", "")
                    if expiration:
                        dt = datetime.fromtimestamp(expiration)
                        expiration = dt.strftime('%Y-%m-%d %H:%M:%S')
                    msg = f"WhatsApp Call for {user_phone} permission changed: {responce} {expiration}"
                    bitrix_tasks.message_add.delay(
                        appinstance.id, 
                        phone.line.line_id,
                        user_phone, 
                        msg, 
                        phone.line.connector.code,
                    )
            if file_url and user_phone:
                attach = [
                    {
                        "url": file_url,
                        "name": filename
                    }
                ]
                bitrix_tasks.send_messages.delay(appinstance.id, user_phone, caption, phone.line.connector.code,
                                                phone.line.line_id, False, user_name, message_id, attach, chat_url=chat_url)

        statuses = value.get("statuses", [])
        if statuses:
            for item in statuses:
                fb_status = item.get("status")
                out_message = None

                if fb_status == "failed":
                    try:
                        out_message = error_message(item)
                        user_phone = item.get("recipient_id")
                        if user_phone and appinstance:
                            bitrix_tasks.message_add.delay(
                                appinstance.id, 
                                phone.line.line_id,
                                user_phone, 
                                f"[color=#ff0000]{out_message}[/color]", 
                                phone.line.connector.code,
                            )
                    except Exception:
                        pass

                biz_opaque_callback_data = item.get("biz_opaque_callback_data")
                if biz_opaque_callback_data:
                    try:
                        callback_data = json.loads(biz_opaque_callback_data)
                        bitrix_user_id = callback_data.get('bitrix_user_id')
                        sms_message_id = callback_data.get('sms_message_id')
                        
                        if fb_status == "failed" and bitrix_user_id:
                            if not out_message:
                                out_message = error_message(item)
                            payload = {"USER_ID": bitrix_user_id, "MESSAGE": out_message}
                            bitrix_tasks.call_api.delay(appinstance.id, "im.notify.system.add", payload)
                        
                        if sms_message_id and fb_status in ["delivered", "failed"]:
                            status_data = {
                                "CODE": phone.line.connector.code,
                                "MESSAGE_ID": sms_message_id,
                                "STATUS": fb_status
                            }
                            bitrix_tasks.call_api.delay(appinstance.id, "messageservice.message.status.update", status_data)
                    except Exception:
                        pass
            return data

        if text and user_phone:
            bitrix_tasks.send_messages.delay(appinstance.id, user_phone, text, phone.line.connector.code,
                                                phone.line.line_id, False, user_name, message_id, chat_url=chat_url)

    elif field == 'smb_message_echoes':
        text = None
        attach= None
        message_echoes = value.get("message_echoes", {})
        for message in message_echoes:
            user_phone = message.get("to")
            message_type = message.get("type")
            if message_type == "text":
                text = message.get("text", {}).get("body")

            elif message_type in ["image", "video", "audio", "document"]:
                media_data = message.get(message_type)
                media_id = media_data["id"]
                media_url = media_data.get("url")
                if not media_url:
                    try:
                        media_info = call_api(waba=phone.waba, endpoint=media_id)
                        media_url = media_info.get("url")
                    except Exception:
                        pass

                extension = media_data["mime_type"].split("/")[1].split(";")[0]
                filename = f"wamid.{media_id}.{extension}"
                
                text = media_data.get("caption") or ""
                original_filename = media_data.get("filename")
                if original_filename:
                    text = f"{original_filename} {text}"
                text = text.strip() if text else None

                # For echo (system) messages, we must upload to Bitrix to keep the file durable in chat history
                try:
                    downloaded = call_api(file_url=media_url, waba=phone.waba)
                    if downloaded:
                        file_url = bitrix_utils.upload_and_get_link(
                            appinstance, downloaded.content, filename
                        )
                    else:
                        file_url = None
                except requests.RequestException:
                    raise
                except Exception as e:
                    file_url = None
                    msg = f"file not downloaded: {e}"
                    if text:
                        text = f"{text} ({msg})"
                    else:
                        text = f"{filename} ({msg})"

                if file_url:
                    attach = [
                        {
                            "FILE": {
                                "NAME": filename,
                                "LINK": file_url
                            }
                        }
                    ]
            if text or attach:
                bitrix_tasks.message_add.delay(appinstance.id, phone.line.line_id, user_phone, text, phone.line.connector.code, attach)
    else:
        raise Exception(f"this event is not handled: {data}")


@shared_task(queue='waba')
def save_approved_templates(id):
    try:
        waba = Waba.objects.get(id=id)
        templates_data = call_api(waba=waba, endpoint=f"{waba.waba_id}/message_templates")

        approved_templates = [
            template for template in templates_data.get("data", [])
            if template.get("status") == "APPROVED"
        ]
        
        for template in approved_templates:
            template_id = template.get("id")
            name = template.get("name")
            lang = template.get("language")
            content = template.get("components")
            status = template.get("status")

            # Создание или обновление шаблона в базе данных
            Template.objects.update_or_create(
                id=template_id,
                defaults={
                    "waba": waba,
                    "owner": waba.owner,
                    "name": name,
                    "lang": lang,
                    "content": content,
                    "status": status,
                }
            )
    except Exception:
        raise
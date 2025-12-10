import base64
import json
import logging
from datetime import datetime

import requests
from django.utils import timezone
from django.conf import settings
from celery import shared_task

from separator.bitrix.models import Line
import separator.bitrix.tasks as bitrix_tasks

from separator.chatwoot.tasks import send_to_chatwoot

from .models import App, Phone, Waba, Template, Event, Error

API_URL = settings.FACEBOOK_API_URL

logger = logging.getLogger("django")


def call_api(app: App=None, waba: Waba=None, endpoint: str=None, method="get", payload=None, file_url=None):
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
            resp = requests.post(f"{base_url}/{endpoint}", json=payload, headers=headers)

        resp_data = resp.json()
        if "error" in resp_data:
            raise Exception(resp_data)

        return resp_data
    except Exception:
        raise


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


def get_file(media_id, filename, appinstance, storage_id, waba):
    try:
        file_data = call_api(waba=waba, endpoint=media_id)
        file_url = file_data.get("url", None)
        download_file = call_api(file_url=file_url, waba=waba)
    except Exception:
        return None
    fileContent = base64.b64encode(download_file.content).decode("utf-8")

    payload = {
        "id": storage_id,
        "fileContent": fileContent,
        "data": {"NAME": f"{media_id}_{filename}"},
    }

    upload_to_bitrix = bitrix_tasks.call_api(appinstance.id, "disk.storage.uploadfile", payload)
    if "result" in upload_to_bitrix:
        return upload_to_bitrix["result"]["DOWNLOAD_URL"]
    else:
        return None


def format_contacts(contacts):
    contact_text = "Присланы контакты:\n"
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

    error_obj, created = Error.objects.get_or_create(
        code=code,
        defaults={"message": fb_message, "details": fb_details}
    )
    out_message = f"Error for: {data.get('recipient_id')}:\n" \
                f"{error_obj.message or fb_message}\n" \
                f"{error_obj.details or fb_details}"
    return out_message


def extract_waba_id(data):
    entry = data.get("entry", [{}])[0]
    waba_id = entry.get("id")
    changes = entry.get("changes", [{}])
    if changes:
        value = changes[0].get("value", {})
        if "waba_info" in value:
            waba_id = value["waba_info"].get("waba_id", waba_id)
    return waba_id


@shared_task(queue='waba')
def event_processing(data):
    waba_id = extract_waba_id(data)
    waba = Waba.objects.filter(waba_id=waba_id).first()
    if waba:
        if waba.app and waba.app.events:
            Event.objects.create(waba=waba, content=json.dumps(data, ensure_ascii=False))
    else:
        if settings.SAVE_UNBOUND_WABA_EVENTS:
            Event.objects.create(content=json.dumps(data, ensure_ascii=False))
    entry = data["entry"][0]
    changes = entry["changes"][0]
    field = changes.get('field')
    value = changes.get('value', {})
    event = value.get('event')

    if field == 'account_update':
        if event == "PARTNER_APP_UNINSTALLED":
            if waba:
                waba.delete()
            return(data)
        elif event == "PHONE_NUMBER_REMOVED":
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
        else:
            raise Exception(data)
    
    metadata = value.get("metadata", {})
    if metadata:    
        phone_number = metadata.get('display_phone_number')
        phone_number_id = metadata.get('phone_number_id')
        try:
            phone = Phone.objects.get(phone_id=phone_number_id, waba=waba)
            appinstance = phone.app_instance
            storage_id = appinstance.storage_id
        except Phone.DoesNotExist:
            raise Exception(f"phone_number not found: {data}")
        except Exception:
            raise Exception(data)

        if settings.CHATWOOT_ENABLED and (not phone.date_end or timezone.now() < phone.date_end):
            # send message to chatwoot 
            send_to_chatwoot.delay(data, phone_number)

    if field == 'message_template_status_update':
        return message_template_status_update(entry)    

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
            if status and phone.calling != status:
                phone.calling = status
                phone.save()
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
        name = None
        for message in messages:
            message_type = message.get("type")
            user_phone = message["from"]
            message_id = message["id"]
            contacts = value.get("contacts", [])
            if contacts:
                name = contacts[0].get("profile", {}).get("name")

            if message_type == "text":
                text = message["text"]["body"]

            elif message_type in ["image", "video", "audio", "document"]:
                media_data = value["messages"][0][message_type]
                media_id = media_data["id"]
                extension = media_data["mime_type"].split("/")[1].split(";")[0]
                filename = media_data.get("filename", f"{media_id}.{extension}")
                caption = media_data.get("caption", None)

                file_url = get_file(
                    media_id, filename, appinstance, storage_id, phone.waba
                )

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
                    text = f"WhatsApp Call for {user_phone} permission changed: {responce} {expiration}"
            if file_url and user_phone:
                attach = [
                    {
                        "url": file_url,
                        "name": filename
                    }
                ]
                bitrix_tasks.send_messages.delay(appinstance.id, user_phone, caption, phone.line.connector.code,
                                                phone.line.line_id, False, name, message_id, attach)

        statuses = value.get("statuses", [])
        if statuses:
            for item in statuses:
                if item.get("status") == "failed":
                    # user_id
                    biz_opaque_callback_data = item.get("biz_opaque_callback_data")
                    if biz_opaque_callback_data:
                        bitrix_user_id = json.loads(biz_opaque_callback_data).get('bitrix_user_id')                        
                        if bitrix_user_id:
                            out_message = error_message(item)
                            payload = {"USER_ID": bitrix_user_id, "MESSAGE": out_message}
                            bitrix_tasks.call_api.delay(appinstance.id, "im.notify.system.add", payload)
                    raise Exception(data)

        if text and user_phone:
            bitrix_tasks.send_messages.delay(appinstance.id, user_phone, text, phone.line.connector.code,
                                                phone.line.line_id, False, name, message_id)
    
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
                extension = media_data["mime_type"].split("/")[1].split(";")[0]
                filename = media_data.get("filename", f"{media_id}.{extension}")
                text = media_data.get("caption", None)
                file_url = get_file(
                    media_id, filename, appinstance, storage_id, phone.waba
                )
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
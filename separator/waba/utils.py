import base64
import logging
from datetime import datetime

import requests
from rest_framework.response import Response
from django.utils import timezone
from django.shortcuts import  get_object_or_404
from django.conf import settings
from celery import shared_task

from separator.bitrix.models import Line
import separator.bitrix.tasks as bitrix_tasks

from separator.chatwoot.tasks import send_to_chatwoot

from .models import App, Phone, Waba, Template

API_URL = settings.FACEBOOK_API_URL
WABA_APP_ID = settings.WABA_APP_ID

logger = logging.getLogger("django")


def get_app():
    return get_object_or_404(App, id=WABA_APP_ID)

def call_api(waba: Waba=None, endpoint: str=None, method="get", payload=None, file_url=None):
    try:
        if waba:
            access_token = waba.access_token
            app = waba.app
        else:
            app = get_app()
            access_token = app.access_token
    except Exception:
        raise
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
            raise Exception(f"Payload: {payload}, Endpoint: {endpoint}, API Error: {resp_data}")

        return resp
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
        return None

    if not phone or not waba:
        return None

    if phone.date_end and timezone.now() > phone.date_end:
        return Response({"error": "phone tariff ended"})
    if not phone.phone_id:
        return None
    call_api(waba=waba, endpoint=f"{phone.phone_id}/messages", method="post", payload=message)


def get_file(media_id, filename, appinstance, storage_id, waba):
    try:
        file_data = call_api(waba=waba, endpoint=media_id)
        file_url = file_data.json().get("url", None)
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
        waba = Waba.objects.get(waba_id=waba_id)
        if waba:
            components = None
            try:
                resp = call_api(waba_id, template_id)
                temp_data = resp.json()
                components = temp_data.get('components')[0]
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
            template = Template.objects.get(id=template_id)
            template.status = event
            template.save()
        except Template.DoesNotExist:
            return Response({"error": "Template not found"})

    return Response({"this event is handled"})

@shared_task(queue='waba')
def event_processing(data):
    entry = data["entry"][0]
    waba_id = entry.get('id')
    changes = entry["changes"][0]
    field = changes.get('field')
    value = changes.get('value', {})
    event = value.get('event')

    metadata = value.get("metadata", {})
    if metadata:    
        phone_number = metadata.get('display_phone_number')
        phone_number_id = metadata.get('phone_number_id')
        try:
            phone = Phone.objects.get(phone_id=phone_number_id)
        except Phone.DoesNotExist:
            logger.error(f"Phone {phone_number} - {phone_number_id} not found")
            raise Exception(f"phone_number not found: {data}")

        if settings.CHATWOOT_ENABLED and (not phone.date_end or timezone.now() < phone.date_end):
            # send message to chatwoot 
            send_to_chatwoot.delay(data, phone_number)

    if field == 'message_template_status_update':
        return message_template_status_update(entry)
    # elif field == 'account_update':
    #     if event == "PHONE_NUMBER_ADDED"
    elif field == 'messages':
        if phone.date_end and timezone.now() > phone.date_end:
            raise Exception(f"phone tariff ended: {data}")

        if not phone.line or not phone.waba or not phone.app_instance:
            raise Exception(f"phone not connected to b24: {data}")
        appinstance = phone.app_instance
        storage_id = appinstance.storage_id

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
                status_name = item.get("status")
                if status_name == "failed":
                    message_id = item.get("id")
                    errors = item.get("errors", [])
                    error_messages = []
                    for error in errors:
                        error_message = f"FaceBook Error Code: {error['code']}, Title: {error['title']}, Message: {error['error_data']['details']}"
                        error_messages.append(error_message)
                    combined_error_message = " | ".join(error_messages)
                    user_phone = item.get("recipient_id")
                    text = combined_error_message

        if text and user_phone:
            bitrix_tasks.send_messages.delay(appinstance.id, user_phone, text, phone.line.connector.code,
                                                phone.line.line_id, False, name, message_id)
    else:
        raise Exception(f"this event is not handled: {data}")


def sample_template(waba: Waba):
    payload = {
        "name": "hello_separator",
        "category": "MARKETING",
        "allow_category_change": True,
        "language": "en_US",
        "components": [
            {
            "type": "BODY",
            "text": "Hello, separator!"
            }
        ]
    }
    return call_api(waba=waba, endpoint=f"{waba.waba_id}/message_templates", method="post", payload=payload)

@shared_task(queue='waba')
def save_approved_templates(id):
    try:
        waba = Waba.objects.get(id=id)
        template_resp = call_api(waba=waba, endpoint=f"{waba.waba_id}/message_templates")
        templates_data = template_resp.json()

        approved_templates = [
            template for template in templates_data.get("data", [])
            if template.get("status") == "APPROVED"
        ]
        if not any(t.get("name") == "hello_separator" for t in approved_templates):
            sample_template(waba)
        
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
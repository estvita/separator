import base64
import logging

import requests
from rest_framework import status
from rest_framework.response import Response
from django.utils import timezone
from django.conf import settings

from thoth.bitrix.crest import call_method
from thoth.bitrix.models import Line
import thoth.bitrix.tasks as bitrix_tasks

from thoth.chatwoot.tasks import send_to_chatwoot

from .models import Phone, Waba, Template

API_URL = 'https://graph.facebook.com/v20.0/'

logger = logging.getLogger("django")

def send_whatsapp_message(access_token, phone_number_id, to, message):
    url = f"{API_URL}{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        **message,
    }
    return requests.post(url, json=payload, headers=headers)


def send_message(appinstance, message, line_id, phone_number):
    # Найти объект Line по line_id и домену
    line = Line.objects.filter(line_id=line_id, app_instance=appinstance).first()

    if not line:
        logger.error(f"Line ID {line_id} not found")
        return Response({f"Line ID {line_id} not found"})

    # Найти объект Waba, связанный с этим Line
    waba = Waba.objects.filter(phones__line=line).first()
    if not waba:
        return None

    access_token = waba.access_token
    # Найти номер телефона, связанный с Line
    phone = waba.phones.filter(line=line).first()
    if phone.date_end and timezone.now() > phone.date_end:
        return Response({"error": "phone tariff ended"})
    phone_id = phone.phone_id if phone else None

    if not phone_id:
        return None

    response = send_whatsapp_message(access_token, phone_id, phone_number, message)
    if response.status_code != 200:
        error = response.json()
        logger.error(f"Failed to send message to {phone}: {error}")
        return response
    else:
        return Response({f"Message sent to {phone}"}, status=status.HTTP_200_OK)


def get_file(access_token, media_id, filename, appinstance, storage_id):
    headers = {"Authorization": f"Bearer {access_token}"}

    file_data = requests.get(f"{API_URL}{media_id}", headers=headers)
    if file_data.status_code != 200:
        return None
    file_url = file_data.json().get("url", None)
    download_file = requests.get(file_url, headers=headers)
    if download_file.status_code != 200:
        return None
    fileContent = base64.b64encode(download_file.content).decode("utf-8")

    payload = {
        "id": storage_id,
        "fileContent": fileContent,
        "data": {"NAME": f"{media_id}_{filename}"},
    }

    upload_to_bitrix = call_method(appinstance, "disk.storage.uploadfile", payload)
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
            url = f"{API_URL}{template_id}"
            headers = {"Authorization": f"Bearer {waba.access_token}"}
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                return Response({"this event is handled"})
            temp_data = resp.json()
            components = temp_data.get('components')[0]

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


def message_processing(request):
    data = request.data
    entry = data["entry"][0]
    changes = entry["changes"][0]
    field = changes.get('field')
    value = changes.get('value', {})

    metadata = value.get("metadata", {})
    if metadata:    
        phone_number = metadata.get('display_phone_number')
        phone_number_id = metadata.get('phone_number_id')
        try:
            phone = Phone.objects.get(phone_id=phone_number_id)
        except Phone.DoesNotExist:
            logger.error(f"Phone {phone_number} - {phone_number_id} not found")
            return Response({"phone_number not found"})

        if settings.CHATWOOT_ENABLED and (not phone.date_end or timezone.now() < phone.date_end):
            # send message to chatwoot 
            send_to_chatwoot.delay(data, phone_number)

    if field == 'message_template_status_update':
        return message_template_status_update(entry)
    elif field != 'messages':
        return Response({"this event is not handled"})
    if phone.date_end and timezone.now() > phone.date_end:
        return Response({"error": "phone tariff ended"})

    if not phone.line or not phone.waba or not phone.app_instance:
        return Response({"error": "phone not connected to b24"})
    appinstance = phone.app_instance
    access_token = phone.waba.access_token
    storage_id = appinstance.storage_id

    messages = value.get("messages", [])
    filename = None
    file_url = None
    text = None
    for message in messages:
        message_type = message.get("type")
        user_phone = message["from"]
        message_id = message["id"]
        contacts = value.get("contacts", [])
        name = None
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
                access_token, media_id, filename, appinstance, storage_id
            )

        elif message_type == "contacts":
            contacts = value["messages"][0]["contacts"]
            text = format_contacts(contacts)

        if text:
            bitrix_tasks.send_messages.delay(appinstance.id, user_phone, text, phone.line.connector.code,
                                             phone.line.line_id, False, name, message_id)
        if file_url:
            attach = [
                {
                    "url": file_url,
                    "name": filename
                }
            ]
            bitrix_tasks.send_messages.delay(appinstance.id, user_phone, caption, phone.line.connector.code,
                                             phone.line.line_id, False, name, message_id, attach)

    # statuses = value.get("statuses", [])
    # # статусы пока на заглушке, ими нечего делать
    # for item in statuses:
    #     status_name = item.get("status")
    #     if status_name == "failed":
    #         message_id = item.get("id")
    #         errors = item.get("errors", [])
    #         logger.error(f"FaceBook Error: {errors}")
    #         error_messages = []
    #         for error in errors:
    #             error_message = f"FaceBook Error Code: {error['code']}, Title: {error['title']}, Message: {error['error_data']['details']}"
    #             error_messages.append(error_message)
    #         combined_error_message = " | ".join(error_messages)
    #         phone = item.get("recipient_id")
    #         text = combined_error_message


def save_approved_templates(waba, owner, templates_data):

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
                "owner": owner,
                "name": name,
                "lang": lang,
                "content": content,
                "status": status,
            }
        )

def sample_template(access_token, waba_id):
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{API_URL}{waba_id}/message_templates"
    payload = {
        "name": "hello_thoth",
        "category": "MARKETING",
        "allow_category_change": True,
        "language": "en_US",
        "components": [
            {
            "type": "BODY",
            "text": "Hello, Thoth!"
            }
        ]
    }

    return requests.post(url, json=payload, headers=headers)
import json
import hmac
import redis
import logging
import hashlib
import re
import os
from urllib.parse import urlparse
from datetime import datetime

import requests
from django.db import OperationalError, models
from django.utils import timezone
from django.utils.translation import gettext as _
from django.conf import settings
from celery import shared_task

from separator.bitrix.models import Line
import separator.bitrix.tasks as bitrix_tasks
import separator.bitrix.utils as bitrix_utils

from .models import (
    App,
    Phone,
    Waba,
    Template,
    Event,
    Error,
    TemplateComponent,
    TemplateComponentButton,
    TemplateComponentNamedParam,
    TemplateComponentPositionalParam,
    TemplateBroadcastRecipient,
    TemplateBroadcast,
)

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
        elif method == "delete":
            resp = requests.delete(f"{base_url}/{endpoint}", params=payload, headers=headers)
        
        return resp.json() if resp.content else {}
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
        response = call_api(waba=waba, endpoint=f"{phone.phone_id}/messages", method="post", payload=message)
        
        if message.get("type") != "template":
            text = ""
            if message.get("type") == "text":
                text = message.get("text", {}).get("body", "")
            
            if text and response and "messages" in response and len(response["messages"]) > 0:
                msg_id = response["messages"][0]["id"]
                try:
                    redis_client.set(f"wamid:{msg_id}", text, ex=600)
                except Exception:
                    pass
                    
        return response
    except Exception as e:
        return {"error": True, "message": str(e)}


def delete_template_remote(template):
    waba = template.waba
    if not waba:
        return {"error": True, "message": "waba not found"}

    endpoint = f"{waba.waba_id}/message_templates"
    attempts = [
        {"name": template.name},
        {"name": template.name, "hsm_id": template.id},
        {"name": template.name, "template_id": template.id},
    ]

    last_error = None
    for params in attempts:
        try:
            return call_api(waba=waba, endpoint=endpoint, method="delete", payload=params)
        except Exception as e:
            last_error = e

    return {"error": True, "message": str(last_error) if last_error else "delete failed"}


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


def fetch_and_save_template(waba, template_id, template_name, lang, event_status=None, components=None):
    status = event_status
    if components is None:
        try:
            temp_data = call_api(waba=waba, endpoint=template_id)
            components = temp_data.get('components')
            if not status:
                status = temp_data.get('status')
        except Exception:
            pass

    Template.objects.filter(id=template_id).delete()

    template = Template.objects.create(
        id=template_id,
        waba=waba,
        owner=waba.owner,
        name=template_name,
        lang=lang,
        content=components,
        status=status
    )
    save_template_components(template, components)
    return template


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
            fetch_and_save_template(waba, template_id, template_name, lang, event)

    elif event == 'PENDING_DELETION':
        try:
            template = Template.objects.filter(id=template_id, waba__waba_id=waba_id).first()
            if template:
                template.delete()
        except Template.DoesNotExist:
            raise Exception(f"Template not found: {entry}")

    else:
        raise Exception(entry)

def message_template_components_update(entry):
    waba_id = entry.get('id')
    changes = entry.get('changes', [])[0]
    value = changes.get('value', {})
    template_id = value.get('message_template_id')
    template_name = value.get('message_template_name')
    lang = value.get('message_template_language')
    
    waba = Waba.objects.filter(waba_id=waba_id).first()
    if waba:
        fetch_and_save_template(waba, template_id, template_name, lang)
    return True
    

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


def _normalize_type(value):
    if not value:
        return None
    return str(value).upper()


def _normalize_format(value):
    if not value:
        return None
    return str(value).upper()


def _extract_named_params(component):
    example = component.get("example") or {}
    named = []
    for key in ("header_text_named_params", "body_text_named_params"):
        items = example.get(key) or []
        for item in items:
            name = item.get("param_name")
            ex = item.get("example")
            if name:
                named.append((name, ex))
    return named


def _extract_positional_params(component):
    example = component.get("example") or {}
    values = []
    for key in ("header_text", "body_text"):
        item = example.get(key)
        if not item:
            continue
        if isinstance(item, list) and item and isinstance(item[0], list):
            values.extend(item[0])
        elif isinstance(item, list):
            values.extend(item)
        else:
            values.append(item)
    return values


def _safe_text(value):
    if value is None:
        return None
    return str(value)


def save_template_components(template, components):
    if components is None:
        return

    TemplateComponent.objects.filter(template=template).delete()

    for comp_index, comp in enumerate(components):
        comp_type = _normalize_type(comp.get("type"))
        comp_format = _normalize_format(comp.get("format"))
        comp_text = comp.get("text")

        component = TemplateComponent.objects.create(
            template=template,
            type=comp_type,
            format=comp_format,
            text=comp_text,
            index=comp_index,
        )

        for name, ex in _extract_named_params(comp):
            TemplateComponentNamedParam.objects.create(
                component=component,
                name=name,
                example=_safe_text(ex),
            )

        for pos_index, ex in enumerate(_extract_positional_params(comp), start=1):
            TemplateComponentPositionalParam.objects.create(
                component=component,
                position=pos_index,
                example=_safe_text(ex),
            )

        if comp_type == "BUTTONS":
            for btn_index, btn in enumerate(comp.get("buttons") or []):
                btn_type = _normalize_type(btn.get("type"))
                btn_text = btn.get("text")
                btn_url = btn.get("url")
                btn_phone = btn.get("phone_number")
                btn_example = btn.get("example")
                if isinstance(btn_example, (list, dict)):
                    btn_example = json.dumps(btn_example, ensure_ascii=True)

                button = TemplateComponentButton.objects.create(
                    component=component,
                    type=btn_type,
                    text=btn_text,
                    url=btn_url,
                    phone_number=btn_phone,
                    example=_safe_text(btn_example),
                    index=btn_index,
                )

                if btn_type == "URL":
                    ex_list = btn.get("example") or []
                    if isinstance(ex_list, list) and ex_list:
                        TemplateComponentPositionalParam.objects.create(
                            component=component,
                            button=button,
                            position=1,
                            example=_safe_text(ex_list[0]),
                        )


def _media_param_type(fmt):
    fmt = _normalize_format(fmt)
    if fmt == "IMAGE":
        return "image"
    if fmt == "VIDEO":
        return "video"
    if fmt == "DOCUMENT":
        return "document"
    if fmt == "GIF":
        return "video"
    return None


def _bitrix_param_example(value, default="123"):
    value = str(value).strip() if value is not None else ""
    if not value:
        value = default
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = value.replace("|", "/")
    return value


def _bitrix_param_name(name, default="param"):
    clean_name = str(name).strip() if name is not None else ""
    if not clean_name:
        clean_name = default
    clean_name = re.sub(r"[\r\n\t]+", " ", clean_name)
    clean_name = clean_name.replace("|", "_").replace(":", "_")
    return clean_name


def _bitrix_file_link_example(fmt):
    fmt = _normalize_format(fmt)
    if fmt == "IMAGE":
        return "https://example.com/image.jpg"
    if fmt == "VIDEO" or fmt == "GIF":
        return "https://example.com/video.mp4"
    return "https://example.com/file.pdf"


def _extract_button_dynamic_param(button_url, button_example):
    url = str(button_url or "")
    example = str(button_example or "")
    if not url or not example:
        return button_example

    placeholder = "{{1}}"
    if placeholder not in url:
        return button_example

    prefix, suffix = url.split(placeholder, 1)
    if example.startswith(prefix) and (not suffix or example.endswith(suffix)):
        end = len(example) - len(suffix) if suffix else len(example)
        return example[len(prefix):end]
    return button_example


def build_bitrix_template_code(template):
    base = f"template+{template.id}"
    payload_segments = []

    for component in template.components.order_by("index", "id"):
        comp_type = _normalize_type(component.type)
        comp_format = _normalize_format(component.format)

        if comp_type in ("HEADER", "BODY"):
            for named in component.named_params.order_by("id"):
                payload_segments.append(
                    f"{_bitrix_param_name(named.name)}:{_bitrix_param_example(named.example)}"
                )
            for positional in component.positional_params.order_by("position", "id"):
                payload_segments.append(_bitrix_param_example(positional.example))

        if comp_type == "HEADER" and comp_format in ("IMAGE", "VIDEO", "DOCUMENT", "GIF"):
            payload_segments.append(f"file_link:{_bitrix_file_link_example(comp_format)}")

        if comp_type == "BUTTONS":
            for button in component.buttons.order_by("index", "id"):
                if _normalize_type(button.type) != "URL":
                    continue

                has_button_params = (
                    button.positional_params.exists()
                    or button.named_params.exists()
                    or (button.url and "{{" in button.url and "}}" in button.url)
                )
                if not has_button_params:
                    continue

                button_value = None
                positional = button.positional_params.order_by("position", "id").first()
                if positional and positional.example:
                    button_value = positional.example
                else:
                    named = button.named_params.order_by("id").first()
                    if named and named.example:
                        button_value = named.example
                    elif button.example:
                        button_value = button.example

                button_value = _extract_button_dynamic_param(button.url, button_value)
                payload_segments.append(f"button_param:{_bitrix_param_example(button_value)}")
                break

    if not payload_segments:
        return base
    return f"{base}+{'|'.join(payload_segments)}"




def build_template_components_payload(template, post_data, files_data, phone):
    components_payload = []
    for component in template.components.order_by("index", "id"):
        comp_type = _normalize_type(component.type)
        comp_format = _normalize_format(component.format)

        if comp_type in ("BODY", "HEADER"):
            params = []
            for p in component.named_params.order_by("id"):
                key = f"param__{component.id}__named__{p.name}"
                val = post_data.get(key)
                if val:
                    params.append({"type": "text", "parameter_name": p.name, "text": val})
            for p in component.positional_params.order_by("position", "id"):
                key = f"param__{component.id}__pos__{p.position}"
                val = post_data.get(key)
                if val:
                    params.append({"type": "text", "text": val})

            if comp_type == "BODY" and params:
                components_payload.append({"type": "body", "parameters": params})
            if comp_type == "HEADER" and comp_format == "TEXT" and params:
                components_payload.append({"type": "header", "parameters": params})

        if comp_type == "HEADER" and comp_format in ("IMAGE", "VIDEO", "DOCUMENT", "GIF"):
            media_key = f"media__{component.id}"
            file_obj = files_data.get(media_key)
            file_url = (post_data.get(media_key) or "").strip()
            media_content = None
            media_type = None
            media_name = None

            if file_obj:
                media_content = file_obj.read()
                media_type = file_obj.content_type or "application/octet-stream"
                media_name = file_obj.name
            elif file_url:
                parsed = urlparse(file_url)
                if parsed.scheme not in ("http", "https"):
                    raise Exception("Media URL must be http/https")
                try:
                    resp = requests.get(file_url, timeout=20, stream=True)
                    resp.raise_for_status()
                except Exception as exc:
                    raise Exception(f"Media download failed: {exc}") from exc

                max_bytes = 25 * 1024 * 1024
                total = 0
                chunks = []
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise Exception("Media download превышает лимит 25MB")
                    chunks.append(chunk)
                media_content = b"".join(chunks)
                media_type = resp.headers.get("Content-Type") or "application/octet-stream"
                path = parsed.path or ""
                media_name = os.path.basename(path) or "media"

            if media_content:
                phone_num = (phone.phone or "").lstrip("+")
                upload = upload_media(
                    phone.app_instance,
                    media_content,
                    media_type,
                    media_name,
                    phone_num=phone_num,
                )
                if upload.get("error"):
                    raise Exception(upload.get("message") or "Media upload failed")
                media_id = upload.get("id")
                if not media_id:
                    raise Exception("Media upload failed: missing id")
                param_type = _media_param_type(comp_format)
                if param_type:
                    components_payload.append({
                        "type": "header",
                        "parameters": [
                            {"type": param_type, param_type: {"id": media_id}}
                        ]
                    })

        if comp_type == "HEADER" and comp_format == "LOCATION":
            lat = post_data.get(f"location__{component.id}__latitude")
            lng = post_data.get(f"location__{component.id}__longitude")
            name = post_data.get(f"location__{component.id}__name")
            address = post_data.get(f"location__{component.id}__address")
            if lat and lng:
                components_payload.append({
                    "type": "header",
                    "parameters": [
                        {
                            "type": "location",
                            "location": {
                                "latitude": lat,
                                "longitude": lng,
                                "name": name or "",
                                "address": address or "",
                            },
                        }
                    ],
                })

        if comp_type == "BUTTONS":
            for button in component.buttons.order_by("index", "id"):
                btn_params = []
                for p in button.named_params.order_by("id"):
                    key = f"param__btn__{button.id}__named__{p.name}"
                    val = post_data.get(key)
                    if val:
                        btn_params.append({"type": "text", "parameter_name": p.name, "text": val})
                for p in button.positional_params.order_by("position", "id"):
                    key = f"param__btn__{button.id}__pos__{p.position}"
                    val = post_data.get(key)
                    if val:
                        btn_params.append({"type": "text", "text": val})
                if btn_params:
                    components_payload.append({
                        "type": "button",
                        "sub_type": (button.type or "").lower(),
                        "index": str(button.index),
                        "parameters": btn_params,
                    })

    return components_payload


def build_broadcast_text(template, post_data):
    def get_named(comp_id, name):
        return post_data.get(f"param__{comp_id}__named__{name}")

    def get_pos(comp_id, pos):
        return post_data.get(f"param__{comp_id}__pos__{pos}")

    def get_btn_named(btn_id, name):
        return post_data.get(f"param__btn__{btn_id}__named__{name}")

    def get_btn_pos(btn_id, pos):
        return post_data.get(f"param__btn__{btn_id}__pos__{pos}")

    def replace_named(text, comp):
        for p in comp.named_params.order_by("id"):
            val = get_named(comp.id, p.name)
            if val:
                text = text.replace("{{" + p.name + "}}", val)
        return text

    def replace_positional(text, comp):
        for p in comp.positional_params.order_by("position", "id"):
            val = get_pos(comp.id, p.position)
            if val:
                text = text.replace("{{" + str(p.position) + "}}", val)
        return text

    lines = []
    for comp in template.components.order_by("index", "id"):
        ctype = _normalize_type(comp.type)
        cformat = _normalize_format(comp.format)
        text = comp.text or ""

        if ctype in ("HEADER", "BODY", "FOOTER") and text:
            text = replace_named(text, comp)
            text = replace_positional(text, comp)
            lines.append(text)

        if ctype == "HEADER" and cformat in ("IMAGE", "VIDEO", "DOCUMENT", "GIF"):
            lines.append("[HEADER MEDIA]")

        if ctype == "HEADER" and cformat == "LOCATION":
            lat = post_data.get(f"location__{comp.id}__latitude")
            lng = post_data.get(f"location__{comp.id}__longitude")
            name = post_data.get(f"location__{comp.id}__name")
            address = post_data.get(f"location__{comp.id}__address")
            parts = [p for p in [name, address, lat, lng] if p]
            if parts:
                lines.append("LOCATION: " + ", ".join(parts))

        if ctype == "BUTTONS":
            for btn in comp.buttons.order_by("index", "id"):
                btext = btn.text or btn.type or "BUTTON"
                if btn.url:
                    url = btn.url
                    for p in btn.positional_params.order_by("position", "id"):
                        val = get_btn_pos(btn.id, p.position)
                        if val:
                            url = url.replace("{{" + str(p.position) + "}}", val)
                    for p in btn.named_params.order_by("id"):
                        val = get_btn_named(btn.id, p.name)
                        if val:
                            url = url.replace("{{" + p.name + "}}", val)
                    btext = f"{btext} {url}"
                lines.append(f"BUTTON: {btext}")

    return "\n".join([line for line in lines if line])


def _status_rank(status):
    order = {
        "pending": 0,
        "sent": 1,
        "delivered": 2,
        "failed": 3,
        "cancelled": 4,
    }
    return order.get(status, 0)


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
            if appinstance:
                appinstance.host = host
        except Phone.DoesNotExist:
            raise Exception(f"phone_number not found: {data}")
        except Exception:
            raise Exception(data)

    if field == 'message_template_status_update':
        return message_template_status_update(entry)    

    elif field == 'message_template_components_update':
        return message_template_components_update(entry)

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

        # if not phone.line or not phone.waba or not phone.app_instance:
        #     statuses = value.get("statuses", [])
        #     if statuses:
        #         for item in statuses:
        #             fb_status = item.get("status")
        #             wamid = item.get("id")
        #             if wamid and fb_status:
        #                 try:
        #                     recipient = TemplateBroadcastRecipient.objects.filter(wamid=wamid).first()
        #                     if recipient and recipient.status != fb_status:
        #                         update_fields = {"status": fb_status}
        #                         if fb_status == "failed":
        #                             update_fields["error_json"] = item
        #                         TemplateBroadcastRecipient.objects.filter(id=recipient.id).update(**update_fields)
        #                         if fb_status == "delivered":
        #                             TemplateBroadcast.objects.filter(id=recipient.broadcast_id).update(
        #                                 delivered_count=models.F("delivered_count") + 1
        #                             )
        #                 except Exception:
        #                     pass
        #         return data
        #     raise Exception(f"phone not connected to b24: {data}")

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

            elif message_type == "button":
                text = message["button"]["text"]

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

            elif message_type == "reaction":
                 reaction = message.get("reaction")
                 text = reaction.get("emoji")

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
                wamid = item.get("id")
                out_message = None

                if fb_status == "failed":
                    fallback_triggered = False
                    try:
                        error_data = (item.get('errors') or [{}])[0]
                        error_code = error_data.get("code")
                        error_obj = Error.objects.filter(code=error_code).first()
                        if error_obj and error_obj.fallback:
                            saved_text = redis_client.get(f"wamid:{wamid}")
                            if saved_text:
                                saved_text = saved_text.decode('utf-8') if isinstance(saved_text, bytes) else saved_text
                                default_template = Template.objects.filter(waba=phone.waba, default=True).first()
                                if default_template:
                                    user_phone = item.get("recipient_id")
                                    payload = {
                                        "messaging_product": "whatsapp",
                                        "type": "template",
                                        "to": user_phone,
                                        "template": {
                                            "name": default_template.name,
                                            "language": {"code": default_template.lang},
                                            "components": [
                                                {
                                                    "type": "body",
                                                    "parameters": [
                                                        {
                                                            "type": "text",
                                                            "text": saved_text
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    }
                                    resp = call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/messages", method="post", payload=payload)
                                    fallback_triggered = True
                                    if user_phone and appinstance:
                                        if "error" in resp:
                                            msg = f"[color=#ff0000]Error occurred: {resp}[/color]"
                                        else:
                                            msg = f"[color=#00ff00]The message was sent using the default template due to error {error_code}[/color]"
                                        bitrix_tasks.message_add.delay(
                                            appinstance.id, 
                                            phone.line.line_id,
                                            user_phone, 
                                            msg, 
                                            phone.line.connector.code,
                                        )
                    except Exception:
                        pass
                        
                    if not fallback_triggered:
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
                        
                        if fb_status == "failed" and bitrix_user_id and not fallback_triggered:
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
                if wamid and fb_status:
                    try:
                        recipient = TemplateBroadcastRecipient.objects.filter(wamid=wamid).first()
                        if recipient:
                            cur_rank = _status_rank(recipient.status)
                            new_rank = _status_rank(fb_status)
                            should_update = new_rank >= cur_rank
                            if recipient.status == "delivered" and fb_status == "sent":
                                should_update = False
                            if recipient.status == "failed" and fb_status != "failed":
                                should_update = False
                            if should_update and recipient.status != fb_status:
                                update_fields = {"status": fb_status}
                                if fb_status == "failed":
                                    update_fields["error_json"] = item
                                TemplateBroadcastRecipient.objects.filter(id=recipient.id).update(**update_fields)
                                if fb_status == "delivered":
                                    TemplateBroadcast.objects.filter(id=recipient.broadcast_id).update(
                                        delivered_count=models.F("delivered_count") + 1
                                    )
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
            
            elif message_type == "reaction":
                 reaction = message.get("reaction")
                 text = reaction.get("emoji")

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

            fetch_and_save_template(waba, template_id, name, lang, event_status=status, components=content)
    except Exception:
        raise

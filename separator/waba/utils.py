import re
import os
import ast
import json
import hmac
import uuid
import redis
import logging
import hashlib
import requests
import mimetypes
from urllib.parse import urlparse, urlencode
from datetime import datetime

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext as _
from django.conf import settings
from celery import shared_task, chain

from separator.users.models import Message
from separator.bitrix.models import Line
import separator.bitrix.tasks as bitrix_tasks
import separator.bitrix.utils as bitrix_utils

from separator.waba.bot import bot_processor
from separator.waba.retry import RETRY_KWARGS, TRANSIENT_ERRORS

from .models import (
    App,
    Phone,
    Waba,
    Template,
    Event,
    Error,
    Ctwa,
    Bot,
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


class WabaNonRetryableError(Exception):
    pass


def extract_error_data(data):
    if not isinstance(data, dict):
        return {}

    if isinstance(data.get("error"), dict):
        return data["error"]

    errors = data.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return errors[0]

    message = data.get("message")
    candidates = []
    if isinstance(message, dict):
        candidates.append(message)
    elif isinstance(message, str):
        candidates.append(message)
        if ": " in message:
            candidates.append(message.split(": ", 1)[1])

    for candidate in candidates:
        if isinstance(candidate, dict):
            if isinstance(candidate.get("error"), dict):
                return candidate["error"]
            return candidate

        for parser in (ast.literal_eval, json.loads):
            try:
                parsed = parser(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                if isinstance(parsed.get("error"), dict):
                    return parsed["error"]
                return parsed

    return {}


def save_error_data(error_data):
    code = error_data.get("code")
    if code is None:
        return None

    fb_message = error_data.get("message") or error_data.get("title") or ""
    fb_details = (error_data.get("error_data") or {}).get("details", "")
    error_obj, _ = Error.objects.get_or_create(
        code=code,
        subcode=error_data.get("error_subcode"),
        defaults={
            "type": error_data.get("type"),
            "message": fb_message,
            "details": fb_details,
        },
    )

    update_fields = []
    if error_data.get("type") and error_obj.type != error_data.get("type"):
        error_obj.type = error_data.get("type")
        update_fields.append("type")
    if fb_message and not error_obj.message:
        error_obj.message = fb_message
        update_fields.append("message")
    if fb_details and not error_obj.details:
        error_obj.details = fb_details
        update_fields.append("details")
    if update_fields:
        error_obj.save(update_fields=update_fields)

    return error_obj


def is_retry_enabled_for_error(data):
    error_data = extract_error_data(data)
    if not error_data:
        return True

    try:
        error_obj = save_error_data(error_data)
    except Exception:
        return True

    return True if error_obj is None else error_obj.retry


def _media_cache_key(phone, kind, value):
    if not phone or not phone.phone_id or not value:
        return None
    if isinstance(value, bytes):
        value_hash = hashlib.sha256(value).hexdigest()
    else:
        value_hash = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"waba_media:{phone.phone_id}:{kind}:{value_hash}"


def get_cached_media_id_for_phone(phone, source_url=None, file_content=None):
    cache_key = _media_cache_key(phone, "url", source_url) if source_url else _media_cache_key(phone, "content", file_content)
    if not cache_key:
        return None
    try:
        cached_id = redis_client.get(cache_key)
        return cached_id.decode("utf-8") if cached_id else None
    except Exception:
        return None


def cache_media_id_for_phone(phone, media_id, source_url=None, file_content=None):
    # Meta media IDs are temporary, so keep the cache below the expected media lifetime.
    if not media_id:
        return
    for cache_key in (
        _media_cache_key(phone, "url", source_url),
        _media_cache_key(phone, "content", file_content),
    ):
        if not cache_key:
            continue
        try:
            redis_client.set(cache_key, media_id, ex=29 * 24 * 60 * 60)
        except Exception:
            pass


TEMPLATE_COMPONENT_PREFETCHES = (
    "components__named_params",
    "components__positional_params",
    "components__buttons__named_params",
    "components__buttons__positional_params",
)


def build_embedded_signup_link(request, user, partner_app=None):
    app, request_id, domain, extras = create_embedded_signup_request(request, user, partner_app=partner_app)
    params = {
        'client_id': app.client_id,
        'config_id': app.config_id,
        'response_type': 'code',
        'override_default_response_type': 'true',
        'redirect_uri': f'https://{domain}/waba/callback/',
        'state': request_id,
        'extras': json.dumps(extras)
    }
    return f'https://www.facebook.com/v{app.api_version}.0/dialog/oauth?{urlencode(params)}'


def build_hosted_embedded_signup_link(app):
    params = {
        "app_id": app.client_id,
        "config_id": app.config_id,
    }
    if app.business_app_onboarding:
        params["extras"] = json.dumps({
            "version": app.es_version,
            "sessionInfoVersion": app.session_info_version,
            "featureType": "whatsapp_business_app_onboarding",
        })
    return f"https://business.facebook.com/messaging/whatsapp/onboard/?{urlencode(params)}"


def build_popup_embedded_signup_config(request, user, partner_app=None):
    app, request_id, _domain, extras = create_embedded_signup_request(request, user, partner_app=partner_app)
    return {
        "request_id": request_id,
        "app_id": app.client_id,
        "config_id": app.config_id,
        "api_version": f"v{app.api_version}.0",
        "extras": extras,
    }


def create_embedded_signup_request(request, user, partner_app=None):
    user_id = user.id
    request_id = str(uuid.uuid4())

    if not user_id or not request_id:
        raise ValueError("Invalid signup request")

    domain = request.get_host().split(':')[0]
    app = partner_app.app if partner_app else App.objects.filter(sites__domain__iexact=domain).first()
    if not app:
        raise App.DoesNotExist

    request_data = {'user': user_id, "app": app.client_id, "host": domain}
    if partner_app:
        request_data["partner_app_id"] = str(partner_app.id)
    redis_client.json().set(request_id, "$", request_data)
    redis_client.expire(request_id, 7200)
    extras = {
        "version": app.es_version,
        "sessionInfoVersion": app.session_info_version,
    }
    if app.business_app_onboarding:
        extras["featureType"] = "whatsapp_business_app_onboarding"

    return app, request_id, domain, extras


def prefetch_template_components(queryset):
    if hasattr(queryset, "prefetch_related"):
        existing = set(getattr(queryset, "_prefetch_related_lookups", ()) or ())
        missing = tuple(item for item in TEMPLATE_COMPONENT_PREFETCHES if item not in existing)
        return queryset.prefetch_related(*missing) if missing else queryset
    return queryset


def _sort_by(*fields):
    return lambda item: tuple(getattr(item, field) for field in fields)


def serialize_templates_for_frontend(templates, stringify_ids=False):
    data = []
    for template in prefetch_template_components(templates):
        components_data = []
        components = sorted(template.components.all(), key=_sort_by("index", "id"))
        for component in components:
            buttons_data = []
            buttons = sorted(component.buttons.all(), key=_sort_by("index", "id"))
            for button in buttons:
                buttons_data.append({
                    "id": button.id,
                    "type": button.type,
                    "index": button.index,
                    "named_params": [
                        {"name": p.name}
                        for p in sorted(button.named_params.all(), key=_sort_by("id"))
                    ],
                    "positional_params": [
                        {"position": p.position}
                        for p in sorted(button.positional_params.all(), key=_sort_by("position", "id"))
                    ],
                })
            components_data.append({
                "id": component.id,
                "type": component.type,
                "format": component.format,
                "index": component.index,
                "text": component.text,
                "named_params": [
                    {"name": p.name}
                    for p in sorted(component.named_params.all(), key=_sort_by("id"))
                ],
                "positional_params": [
                    {"position": p.position}
                    for p in sorted(component.positional_params.all(), key=_sort_by("position", "id"))
                ],
                "buttons": buttons_data,
            })
        template_id = str(template.id) if stringify_ids else template.id
        data.append({
            "id": template_id,
            "label": f"{template.name} ({template.lang})",
            "lang": template.lang,
            "components": components_data,
        })
    return data


def call_api(app: App=None, waba: Waba=None, endpoint: str=None, method="get", payload=None, file_url=None, files=None, data=None):
    if not app and waba:
        access_token = waba.access_token
        app = waba.app
    else:
        access_token = app.access_token
    headers = {"Authorization": f"Bearer {access_token}"}
    base_url = f"{API_URL}/v{app.api_version}.0"
    timeout = (10, 60)

    resp = None
    try:
        if file_url:
            return requests.get(file_url, headers=headers, timeout=timeout)
        if method == "get":
            resp = requests.get(f"{base_url}/{endpoint}", params=payload, headers=headers, timeout=timeout)
        elif method == "post":
            if files:
                resp = requests.post(f"{base_url}/{endpoint}", data=data, files=files, headers=headers, timeout=timeout)
            else:
                resp = requests.post(f"{base_url}/{endpoint}", json=payload, headers=headers, timeout=timeout)
        elif method == "delete":
            resp = requests.delete(f"{base_url}/{endpoint}", json=payload, headers=headers, timeout=timeout)
        
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            try:
                error_details = resp.json()
            except Exception:
                error_details = {"error": resp.text}
            raise Exception(error_details) from e
            
        return resp.json() if resp.content else {}
    except Exception:
        raise


def _multipart_stream(boundary, fields, file_name, filename, mime_type, chunks):
    boundary_bytes = boundary.encode("utf-8")
    for name, value in fields.items():
        yield b"--" + boundary_bytes + b"\r\n"
        yield f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        yield str(value).encode("utf-8")
        yield b"\r\n"

    safe_filename = str(filename or "file").replace('"', "")
    yield b"--" + boundary_bytes + b"\r\n"
    yield (
        f'Content-Disposition: form-data; name="{file_name}"; filename="{safe_filename}"\r\n'
        f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n"
    ).encode("utf-8")
    for chunk in chunks:
        if chunk:
            yield chunk
    yield b"\r\n--" + boundary_bytes + b"--\r\n"


def upload_media_url_for_phone(phone, file_url, filename, source_url=None, mime_type=None):
    waba = phone.waba if phone else None
    if not phone or not waba:
        return {"error": True, "message": "not phone or not waba"}

    if phone.date_end and timezone.now() > phone.date_end:
        return {"error": True, "message": "phone tariff expired"}

    if not phone.phone_id:
        return {"error": True, "message": "not phone phone_id"}

    source_url = source_url or file_url
    try:
        cached_id = get_cached_media_id_for_phone(phone, source_url)
        if cached_id:
            return {"id": cached_id}

        app = waba.app
        headers = {"Authorization": f"Bearer {waba.access_token}"}
        base_url = f"{API_URL}/v{app.api_version}.0"
        endpoint = f"{base_url}/{phone.phone_id}/media"
        boundary = f"----separator-{uuid.uuid4().hex}"

        with requests.get(file_url, stream=True, timeout=(10, 60)) as upstream:
            upstream.raise_for_status()
            upload_mime_type = mime_type or upstream.headers.get("Content-Type") or "application/octet-stream"
            body = _multipart_stream(
                boundary,
                {"messaging_product": "whatsapp"},
                "file",
                filename,
                upload_mime_type,
                upstream.iter_content(chunk_size=64 * 1024),
            )
            response = requests.post(
                endpoint,
                data=body,
                headers={
                    **headers,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                timeout=(10, 300),
            )

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            try:
                error_details = response.json()
            except Exception:
                error_details = {"error": response.text}
            raise Exception(error_details) from e

        result = response.json() if response.content else {}
        if isinstance(result, dict) and result.get("id"):
            cache_media_id_for_phone(phone, result["id"], source_url=source_url)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}


def get_hosted_business_token(app: App, owner_business_id: str):
    if not app or not app.access_token or not app.client_secret:
        raise Exception("Hosted app must have system access_token and client_secret")
    if not owner_business_id:
        raise Exception("owner_business_id is required")

    appsecret_proof = hmac.new(
        app.client_secret.encode("utf-8"),
        app.access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    base_url = f"{API_URL}/v{app.api_version}.0"
    resp = requests.post(
        f"{base_url}/{owner_business_id}/system_user_access_tokens",
        data={
            "appsecret_proof": appsecret_proof,
            "fetch_only": "true",
        },
        headers={
            "Authorization": f"Bearer {app.access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        try:
            error_details = resp.json()
        except Exception:
            error_details = resp.text
        raise Exception(f"Hosted token API Error {resp.status_code}: {error_details}") from e

    data = resp.json() if resp.content else {}
    access_token = data.get("access_token")
    if not access_token:
        raise Exception(f"Hosted token response has no access_token: {data}")
    return access_token


def upload_media_for_phone(phone, file_content, mime_type, filename, source_url=None):
    waba = phone.waba if phone else None
    if not phone or not waba:
        return {"error": True, "message": "not phone or not waba"}

    if phone.date_end and timezone.now() > phone.date_end:
        return {"error": True, "message": "phone tariff expired"}

    if not phone.phone_id:
        return {"error": True, "message": "not phone phone_id"}

    try:
        if source_url:
            cached_id = get_cached_media_id_for_phone(phone, source_url)
            if cached_id:
                return {"id": cached_id}

        cached_id = get_cached_media_id_for_phone(phone, file_content=file_content)
        if cached_id:
            cache_media_id_for_phone(phone, cached_id, source_url=source_url)
            return {"id": cached_id}

        files = {
            'file': (filename, file_content, mime_type)
        }
        data = {
            'messaging_product': 'whatsapp'
        }
        result = call_api(waba=waba, endpoint=f"{phone.phone_id}/media", method="post", files=files, data=data)
        if isinstance(result, dict) and result.get("id"):
            cache_media_id_for_phone(phone, result["id"], source_url=source_url, file_content=file_content)
        return result
    except Exception as e:
        return {"error": True, "message": str(e)}


def upload_media(appinstance, file_content, mime_type, filename, line_id=None, phone_num=None, source_url=None):
    phone = None
    if phone_num:
        phone = Phone.objects.filter(phone=f"+{phone_num}").first()
    elif line_id:
        line = Line.objects.filter(line_id=line_id, portal=appinstance.portal).first()
        waba = Waba.objects.filter(phones__line=line).first() if line else None
        phone = waba.phones.filter(line=line).first() if waba and line else None
    else:
        return {"error": True, "message": "phone not found"}

    return upload_media_for_phone(phone, file_content, mime_type, filename, source_url=source_url)


def _template_message_endpoint(phone, message, template=None):
    endpoint = f"{phone.phone_id}/messages"
    if message.get("type") != "template":
        return endpoint

    template_obj = template
    if not template_obj:
        template_data = message.get("template") or {}
        template_name = template_data.get("name")
        template_lang = (template_data.get("language") or {}).get("code")
        template_obj = Template.objects.filter(
            waba=phone.waba,
            name=template_name,
            lang=template_lang,
        ).first()

    if template_obj and (template_obj.category or "").upper() == "MARKETING":
        return f"{phone.phone_id}/marketing_messages"
    return endpoint


def _cache_outbound_text(message, response):
    if message.get("type") != "text":
        return
    text = message.get("text", {}).get("body", "")
    if not text or not response or "messages" not in response or not response["messages"]:
        return
    msg_id = response["messages"][0]["id"]
    try:
        redis_client.set(f"wamid:{msg_id}", text, ex=600)
    except Exception:
        pass


def send_message_from_phone(phone, message, template=None):
    waba = phone.waba if phone else None
    if not phone or not waba:
        return {"error": True, "message": "not phone or not waba"}
    if phone.date_end and timezone.now() > phone.date_end:
        return {"error": True, "message": f"phone {phone} tariff expired"}
    if not phone.phone_id:
        return {"error": True, "message": f"not {phone} phone_id"}
    try:
        endpoint = _template_message_endpoint(phone, message, template=template)
        response = call_api(waba=waba, endpoint=endpoint, method="post", payload=message)
        _cache_outbound_text(message, response)
        return response
    except requests.RequestException:
        raise
    except Exception as e:
        return {"error": True, "message": str(e)}


def send_message(appinstance, message, line_id=None, phone_num=None):
    phone = None
    if phone_num:
        phone = Phone.objects.filter(phone=f"+{phone_num}").first()
    elif line_id:
        line = Line.objects.filter(line_id=line_id, portal=appinstance.portal).first()
        waba = Waba.objects.filter(phones__line=line).first() if line else None
        phone = waba.phones.filter(line=line).first() if waba and line else None
    else:
        return {"error": True, "message": f"phone {phone} not found"}
    return send_message_from_phone(phone, message)


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


def get_file(media_url, filename, appinstance, waba, media_id=None, phone=None):
    if phone and phone.file_proxy and media_id:
        proxy_url = bitrix_utils.build_waba_media_proxy_url(appinstance, phone, media_id, filename)
        if proxy_url:
            return proxy_url

    try:
        download_file = call_api(file_url=media_url, waba=waba)
    except Exception:
        return None
    
    # Use centralized logic for temp file handling
    file_url = bitrix_utils.save_temp_file(download_file.content, filename, appinstance)
    return file_url


def _resolve_media_extension(mime_type, original_filename=None):
    """
    Resolve a file extension for media files.
    Priority:
    1) extension from original filename (if present)
    2) known MIME mapping
    3) Python mimetypes
    4) fallback to subtype part from MIME
    """
    if original_filename:
        ext = os.path.splitext(original_filename)[1].lstrip(".").lower()
        if ext:
            return ext

    mime = (mime_type or "").split(";")[0].strip().lower()
    mime_to_ext = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/msword": "doc",
        "application/vnd.ms-excel": "xls",
        "application/vnd.ms-powerpoint": "ppt",
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
    }
    if mime in mime_to_ext:
        return mime_to_ext[mime]

    guessed = mimetypes.guess_extension(mime, strict=False)
    if guessed:
        return guessed.lstrip(".").lower()

    if "/" in mime:
        return mime.split("/", 1)[1]
    return "bin"


def format_contacts(contacts):
    def _clean(value):
        if value in [None, ""]:
            return None
        return str(value).strip()

    def _join(values, sep=", "):
        return sep.join([item for item in (_clean(v) for v in values) if item])

    def _append(lines, label, value):
        value = _clean(value)
        if value:
            lines.append(f"{label}: {value}")

    def _unique_preserve_order(values):
        seen = set()
        unique_values = []
        for value in values:
            key = _clean(value)
            if not key or key in seen:
                continue
            seen.add(key)
            unique_values.append(key)
        return unique_values

    contact_lines = []
    for i, contact in enumerate(contacts or [], start=1):
        lines = []
        name = contact.get("name") or {}
        formatted_name = _clean(name.get("formatted_name"))
        lines.append(f"{i}. {formatted_name or _('Без имени')}")
        _append(lines, "Birthday", contact.get("birthday"))

        org = contact.get("org") or {}
        _append(lines, "Company", org.get("company"))
        _append(lines, "Department", org.get("department"))
        _append(lines, "Title", org.get("title"))

        for phone_index, phone in enumerate(contact.get("phones") or [], start=1):
            phone_value = _clean(phone.get("phone"))
            wa_id = _clean(phone.get("wa_id"))
            phone_text = phone_value
            if wa_id and phone_text:
                phone_text = f"{phone_text} (WA)"
            if phone_text:
                lines.append(f"Phone {phone_index}: {phone_text}")

        email_values = _unique_preserve_order(
            [email.get("email") for email in (contact.get("emails") or [])]
        )
        for email_index, email_value in enumerate(email_values, start=1):
            lines.append(f"Email {email_index}: {email_value}")

        for url_index, url in enumerate(contact.get("urls") or [], start=1):
            url_value = _clean(url.get("url"))
            url_parts = [url_value]
            url_text = _join(url_parts)
            if url_text:
                lines.append(f"URL {url_index}: {url_text}")

        for address_index, address in enumerate(contact.get("addresses") or [], start=1):
            address_text = _join(
                [
                    address.get("street"),
                    address.get("city"),
                    address.get("state"),
                    address.get("country"),
                    address.get("zip"),
                ]
            )
            if address_text:
                lines.append(f"Address {address_index}: {address_text}")

        contact_lines.append("\n".join(lines))

    return "\n\n".join(contact_lines)


def _format_flow_field(value):
    if isinstance(value, list):
        return ", ".join(_format_flow_field(item) for item in value if item not in [None, ""])

    text = str(value or "").strip()
    text = re.sub(r"^\d+_", "", text)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def _format_nfm_reply(interactive):
    reply = interactive.get("nfm_reply", {})
    response_json = reply.get("response_json")
    if not response_json:
        return reply.get("body") or reply.get("name")

    try:
        data = json.loads(response_json)
    except Exception:
        return response_json

    def _screen_sort_key(item):
        key = str(item[0])
        match = re.search(r"screen_(\d+)", key)
        return (int(match.group(1)) if match else 10**9, key)

    lines = []
    for key, value in sorted(data.items(), key=_screen_sort_key):
        if key == "flow_token":
            continue

        formatted_key = _format_flow_field(key) or key
        formatted_value = _format_flow_field(value)
        if formatted_value:
            lines.append(f"{formatted_key}: {formatted_value}")

    if lines:
        return "\n".join(lines)

    return reply.get("body") or reply.get("name") or response_json


def _build_nfm_reply_attachment(appinstance, interactive, message_timestamp):
    reply = interactive.get("nfm_reply", {})
    response_json = reply.get("response_json")
    if not response_json or not appinstance:
        return None

    filename = f"{message_timestamp or int(datetime.now().timestamp())}.json"
    file_url = bitrix_utils.save_temp_file(
        response_json.encode("utf-8"),
        filename,
        appinstance,
    )
    if not file_url:
        return None

    return [{"url": file_url, "name": filename}]


def fetch_and_save_template(waba, template_id, template_name, lang, event_status=None, components=None, category=None):
    status = event_status
    if components is None:
        try:
            temp_data = call_api(waba=waba, endpoint=template_id)
            components = temp_data.get('components')
            if not status:
                status = temp_data.get('status')
        except Exception:
            pass

    existing_template = Template.objects.filter(id=template_id).only("default", "availableInB24").first()
    is_default = existing_template.default if existing_template else False
    is_available_in_b24 = existing_template.availableInB24 if existing_template else True

    Template.objects.filter(id=template_id).delete()

    template = Template.objects.create(
        id=template_id,
        waba=waba,
        owner=waba.owner,
        category=category or "MARKETING",
        name=template_name,
        lang=lang,
        content=components,
        status=status,
        availableInB24=is_available_in_b24,
        default=is_default,
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
    category = value.get('message_template_category')
    if event == "APPROVED":
        waba = Waba.objects.filter(waba_id=waba_id).first()
        if waba:
            fetch_and_save_template(waba, template_id, template_name, lang, event, category=category)

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


def template_category_update(entry):
    waba_id = entry.get('id')
    changes = entry.get('changes', [])[0]
    value = changes.get('value', {})
    template_id = value.get('message_template_id')
    new_category = value.get('new_category')

    if template_id and new_category:
        Template.objects.filter(id=template_id, waba__waba_id=waba_id).update(category=new_category)
    return True
    

def error_message(data):
    error = extract_error_data(data)
    fb_message = error.get("message") or error.get("title") or ""
    fb_details = (error.get("error_data") or {}).get("details", "")
    recipient = data.get('recipient_id') or data.get('from')

    try:
        error_obj = save_error_data(error)

        if error_obj and error_obj.original:
            return str(data)
        
        out_message = f"Error for: {recipient}:\n" \
                    f"{error_obj.message if error_obj else fb_message}\n" \
                    f"{error_obj.details if error_obj else fb_details}"
        return out_message
    except Exception:
        return f"Error for: {recipient}: {fb_message} {fb_details}"



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


def _extract_text_placeholders(text):
    if not text:
        return []

    placeholders = []
    seen = set()
    for match in re.finditer(r"{{\s*([^{}]+?)\s*}}", str(text)):
        name = match.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        placeholders.append(name)
    return placeholders


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
            media_id = None

            if file_obj:
                media_content = file_obj.read()
                media_type = file_obj.content_type or "application/octet-stream"
                media_name = file_obj.name
            elif file_url:
                parsed = urlparse(file_url)
                if parsed.scheme not in ("http", "https"):
                    raise Exception("Media URL must be http/https")
                # Check URL cache before downloading media from the customer-provided source.
                media_id = get_cached_media_id_for_phone(phone, file_url)
                if not media_id:
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
                            # This message is shown to the user in Bitrix as a send error.
                            raise Exception("Media file exceeds 25MB limit")
                        chunks.append(chunk)
                    media_content = b"".join(chunks)
                    media_type = resp.headers.get("Content-Type") or "application/octet-stream"
                    path = parsed.path or ""
                    media_name = os.path.basename(path) or "media"

            if media_content and not media_id:
                upload = upload_media_for_phone(
                    phone,
                    media_content,
                    media_type,
                    media_name,
                    source_url=file_url,
                )
                if upload.get("error"):
                    raise Exception(upload.get("message") or "Media upload failed")
                media_id = upload.get("id")
                if not media_id:
                    raise Exception("Media upload failed: missing id")

            if media_id:
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

def _sanitize_template_param_text(text):
    if text is None:
        return "---"
    cleaned = str(text).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = re.sub(r" +", " ", cleaned)
    return cleaned.strip()


def _build_fallback_body_parameters(template, text):
    body = template.components.filter(type="BODY").order_by("index", "id").first()
    if body:
        for placeholder in _extract_text_placeholders(body.text):
            if not placeholder.isdigit():
                return [{"type": "text", "parameter_name": placeholder, "text": text}]
    return [{"type": "text", "text": text}]


def send_bitrix_message_from_waba(*args, **kwargs):
    if getattr(settings, "WABA_SEND_BITRIX_MESSAGES_ASYNC", False):
        task = bitrix_tasks.send_messages.delay(*args, **kwargs)
        return {"queued": True, "task_id": task.id}
    return bitrix_tasks.send_messages(*args, **kwargs)


@shared_task(
    queue='waba_messages',
    **RETRY_KWARGS,
)
def messages_processing(raw_body=None, signature=None, app_id=None, host=None):
    return event_processing(raw_body, signature, app_id, host)


@shared_task(
    queue='waba',
    **RETRY_KWARGS,
)
def read_status(waba_id=None, phone_id=None, message_id=None):
    if not waba_id or not phone_id or not message_id:
        return None

    waba = Waba.objects.filter(id=waba_id).first()
    if not waba:
        return None

    status_data = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    return call_api(
        waba=waba,
        endpoint=f"{phone_id}/messages",
        method="post",
        payload=status_data,
    )


def _queue_waba_read_status(waba, phone, message_id):
    if not waba or not getattr(waba, "id", None) or not phone or not getattr(phone, "phone_id", None) or not message_id:
        return
    if not phone.read_receipts:
        return
    try:
        read_status.delay(waba.id, phone.phone_id, message_id)
    except Exception as e:
        logger.warning(f"Failed to queue read status for message {message_id}: {e}")


@shared_task(
    queue='waba',
    **RETRY_KWARGS,
)
def event_processing(raw_body=None, signature=None, app_id=None, host=None):
    if not raw_body:
        raise Exception("No data provided")

    if settings.WABA_VERIFY_SIGNATURE:
        if not signature:
            raise Exception("Missing X-Hub-Signature-256")

        if app_id:
            apps = App.objects.filter(client_id=app_id)
        elif host:
            domains = [host]
            if ':' in host:
                domains.append(host.split(':')[0])
            apps = App.objects.filter(sites__domain__in=domains)
        else:
            apps = []

        verified = False
        payload = raw_body.encode('utf-8')
        signature = signature[7:] if signature.startswith("sha256=") else signature
        for app in apps:
            if not app.client_secret:
                continue
            try:
                secret = app.client_secret.encode('utf-8')
                expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, expected):
                    verified = True
                    break
            except Exception as e:
                logger.error(f"Error verifying signature for app {app.id}: {e}")

        if not verified:
            logger.warning(f"Signature verification failed. Signature: {signature}")
            raise Exception(f"Invalid signature: {signature}")

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from raw_body")
        raise Exception("Invalid JSON")

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
    ctwa_enabled = False
    phone = None
    appinstance = None
    send_result = None

    if field == 'account_update':
        if event == "PARTNER_ADDED":
            waba_info = value.get("waba_info") or {}
            hosted_app_id = app_id
            hosted_waba_id = waba_info.get("waba_id")
            owner_business_id = waba_info.get("owner_business_id")

            app = App.objects.filter(client_id=hosted_app_id, auth_flow=App.AuthFlow.HOSTED).first()
            if not app:
                raise Exception(f"Hosted app not found or disabled: {hosted_app_id}")
            if not hosted_waba_id or not owner_business_id:
                raise Exception(f"Hosted payload is missing waba_id or owner_business_id: {data}")

            from separator.waba.tasks import hosted_partner_added
            hosted_partner_added.delay(app.id, hosted_waba_id, owner_business_id)
            return {
                "status": "scheduled",
                "event": event,
                "app_id": hosted_app_id,
                "waba_id": hosted_waba_id,
            }

        if event == "PARTNER_APP_UNINSTALLED":
            try:
                if waba:
                    if waba.owner_id:
                        numbers = list(
                            waba.phones.exclude(phone__isnull=True)
                            .exclude(phone="")
                            .order_by("phone", "id")
                            .values_list("phone", flat=True)
                        )
                        numbers_text = ", ".join(dict.fromkeys(numbers)) or "no linked numbers"
                        lead_title = bitrix_tasks.build_lead_title(
                            waba.owner.site if waba.owner_id else None,
                            "partner_app_uninstalled",
                            "WABA {waba_id} uninstalled with numbers {numbers}",
                            id=waba.waba_id,
                            waba_id=waba.waba_id,
                            numbers=numbers_text,
                        )
                        bitrix_tasks.prepare_lead.delay(waba.owner_id, lead_title)

                    if settings.WABA_AUTO_DELETE_ENTITIES:
                        waba.delete()
            except Exception as e:
                raise e
        elif event == "PHONE_NUMBER_REMOVED":
            if settings.WABA_AUTO_DELETE_ENTITIES:
                try:
                    phone_number = value.get("phone_number")
                    phone = Phone.objects.filter(phone=f"+{phone_number}", waba=waba).first()
                    if phone:
                        phone.delete()
                        return(f"Phone {phone_number} deleted")
                    else:
                        raise Exception(f"phone_number not found {phone_number}")
                except Exception:
                    raise Exception(data)
        elif event == "PHONE_NUMBER_ADDED":
            if not waba:
                raise
            phone_number = value.get("phone_number")
            if not phone_number:
                raise

            from separator.waba.tasks import add_phone_number_to_waba
            add_phone_number_to_waba.delay(waba.waba_id, phone_number)
            return {
                "status": "scheduled",
                "event": event,
                "waba_id": waba.waba_id,
                "phone_number": phone_number,
            }
        else:
            raise Exception(data)

    if field == 'message_template_status_update':
        return message_template_status_update(entry)

    if field == 'message_template_components_update':
        return message_template_components_update(entry)

    if field == 'template_category_update':
        return template_category_update(entry)
    
    metadata = value.get("metadata", {})
    if metadata:    
        phone_number = metadata.get('display_phone_number')
        phone_number_id = metadata.get('phone_number_id')
        try:
            phone = (
                Phone.objects.select_related("line", "line__connector", "line__portal", "waba", "waba__app")
                .filter(phone_id=phone_number_id, waba=waba)
                .first()
            )
            if not phone:
                raise Exception(f"phone_number not found {phone_number}")
            if phone.line_id:
                try:
                    appinstance = bitrix_utils.get_line_app_instance(phone.line, "waba")
                except bitrix_utils.AppInstance.DoesNotExist:
                    appinstance = None

                if appinstance:
                    appinstance.host = host
                    if appinstance.has_active_feature("separator_ctwa_tracker"):
                        ctwa_enabled = True
        except Exception:
            raise
        
    # ctwa bot
    bot = Bot.objects.filter(phone=phone).first()
    if bot and field == 'messages':
        bot_processor.delay(data, bot.id)

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
            raise
    
    elif field == 'messages':
        if not appinstance or not phone.line_id or not phone.line.connector_id:
            return "WABA phone is not connected to Bitrix line"

        if phone.date_end and timezone.now() > phone.date_end:
            return "subscription ended"

        messages = value.get("messages", [])
        filename = None
        file_url = None
        text = None
        attach = None
        user_name = None
        source_url = None
        ctwa_id = None
        source_id = None
        referral_body = None
        user_identy = None
        for message in messages:
            attach = None
            contacts = value.get("contacts", [])
            if contacts:
                wa_id = contacts[0].get("wa_id")
                if wa_id and not str(wa_id).startswith("+"):
                    wa_id = f"+{wa_id}"
                user_identy = wa_id or contacts[0].get("user_id")
                user_name = contacts[0].get("profile", {}).get("name")
                if user_name:
                    user_name = re.sub(r'[^\w\s\-\']', '', user_name).strip()
            referral = message.get("referral")
            if isinstance(referral, dict):

                # https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging/#ads-that-click-to-whatsapp
                source_type = referral.get("source_type")
                source_id = referral.get("source_id")
                source_url = referral.get("source_url")
                referral_body = referral.get("body")
                ctwa_clid = referral.get("ctwa_clid")

                if ctwa_clid and waba:
                    ctwa, created = Ctwa.objects.get_or_create(
                        clid=ctwa_clid,
                        defaults={
                            "waba": waba,
                            "waba_phone": phone,
                            "phone": user_identy,
                            "source_type": source_type,
                            "source_id": source_id,
                            "source_url": source_url
                        }
                    )
                    if ctwa_enabled:
                        ctwa_id = str(ctwa.id)

            message_type = message.get("type")
            message_id = message["id"]
            message_timestamp = message.get("timestamp")

            if message_type == "text":
                text = message["text"]["body"]

            elif message_type == "button":
                text = message["button"]["text"]

            elif message_type in ["image", "video", "audio", "document", "sticker"]:
                media_data = value["messages"][0][message_type]
                media_id = media_data["id"]
                media_url = media_data.get("url")
                original_filename = media_data.get("filename")
                extension = _resolve_media_extension(media_data.get("mime_type"), original_filename)
                filename = f"wamid.{media_id}.{extension}"
                           
                caption = media_data.get("caption") or ""
                if original_filename:
                    caption = f"{original_filename} {caption}"
                caption = caption.strip() if caption else None
                
                # Store mapping media_id -> message_id in Redis (expire 3 months)
                try:
                    redis_client.set(f"wamid:{media_id}", message_id, ex=7776000)
                except Exception:
                    pass

                file_url = get_file(media_url, filename, appinstance, phone.waba, media_id=media_id, phone=phone)

            elif message_type == "location":
                location = message.get("location", {})
                latitude = location.get("latitude")
                longitude = location.get("longitude")
                name = location.get("name")
                address = location.get("address")
                location_url = location.get("url")
                lines = []
                if name:
                    lines.append(f"Name: {name}")
                if address:
                    lines.append(f"Address: {address}")
                if location_url:
                    lines.append(f"URL: {location_url}")
                if latitude is not None and longitude is not None:
                    maps_url = f"https://www.google.com/maps/place//@{latitude},{longitude},1000m/"
                    lines.append(f"Map: {maps_url}")
                text = "\n".join(lines)

            elif message_type == "system":
                system_data = message.get("system", {})
                text = system_data.get("body")
                if not user_identy:
                    system_wa_id = system_data.get("wa_id") or message.get("from")
                    if system_wa_id and not str(system_wa_id).startswith("+"):
                        system_wa_id = f"+{system_wa_id}"
                    user_identy = system_wa_id

            elif message_type == "order":
                order = message.get("order", {})
                catalog_id = order.get("catalog_id")
                order_text = order.get("text")
                product_items = order.get("product_items") or []
                lines = []
                if order_text:
                    lines.append(order_text)
                if catalog_id:
                    lines.append(f"Catalog ID: {catalog_id}")
                for item in product_items:
                    product_id = item.get("product_retailer_id")
                    quantity = item.get("quantity")
                    price = item.get("item_price")
                    currency = item.get("currency")
                    item_line = f"Product ID: {product_id}"
                    details = []
                    if quantity is not None:
                        details.append(f"qty={quantity}")
                    if price is not None:
                        if currency:
                            details.append(f"price={price} {currency}")
                        else:
                            details.append(f"price={price}")
                    if details:
                        item_line = f"{item_line} ({', '.join(details)})"
                    lines.append(item_line)
                text = "\n".join(lines) if lines else None

            elif message_type == "contacts":
                contacts = message.get("contacts", [])
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
                    msg = f"WhatsApp Call for {user_identy} permission changed: {responce} {expiration}"
                    return send_bitrix_message_from_waba(
                        appinstance.id,
                        user_identy,
                        msg,
                        phone.line.connector.code,
                        phone.line.line_id,
                        manager_id=0,
                    )
                elif interactive_type == "nfm_reply":
                    text = _format_nfm_reply(interactive)
                    attach = _build_nfm_reply_attachment(appinstance, interactive, message_timestamp)
                elif interactive_type == "button_reply":
                    reply = interactive.get("button_reply", {})
                    title = reply.get("title")
                    reply_id = reply.get("id")
                    lines = []
                    if title:
                        lines.append(title)
                    if reply_id:
                        lines.append(f"ID: {reply_id}")
                    text = "\n".join(lines) if lines else None
                elif interactive_type == "list_reply":
                    reply = interactive.get("list_reply", {})
                    title = reply.get("title")
                    description = reply.get("description")
                    reply_id = reply.get("id")
                    lines = []
                    if title:
                        lines.append(title)
                    if description:
                        lines.append(description)
                    if reply_id:
                        lines.append(f"ID: {reply_id}")
                    text = "\n".join(lines) if lines else None
                else:
                    raise Exception(f"Unsupported interactive_type: {interactive_type}")

            elif message_type == "edit":
                edit_data = message.get("edit", {})
                edited_message = edit_data.get("message", {})
                edited_type = edited_message.get("type")
                if edited_type == "text":
                    edited_text = (edited_message.get("text") or {}).get("body")
                    text = f"📝: {edited_text}" if edited_text else "Edited:"
                else:
                    raise Exception(f"Unsupported edit.message type: {edited_type}")

            elif message_type == "reaction":
                 reaction = message.get("reaction")
                 text = reaction.get("emoji")

            elif message_type == "unsupported":
                text = f"[color=#ff0000]{error_message(message)}[/color]"

            else:
                raise Exception(f"Unsupported message type")

            if file_url and user_identy:
                attach = [
                    {
                        "url": file_url,
                        "name": filename
                    }
                ]
                send_result = send_bitrix_message_from_waba(
                    appinstance.id,
                    user_identy,
                    caption,
                    phone.line.connector.code,
                    phone.line.line_id,
                    pushName=user_name,
                    message_id=message_id,
                    attachments=attach,
                    chat_url=source_url,
                )
                _queue_waba_read_status(waba, phone, message_id)
                if message_type == "audio" and media_data.get("voice") is True and media_url:
                    try:
                        app = phone.waba.app if phone.waba_id and phone.waba else None
                        if app and app.openai_api_key and phone.tokens > 0:
                            from separator.waba.tasks import transcribe_voice_message
                            transcribe_voice_message.delay(
                                phone.id,
                                appinstance.id,
                                user_identy,
                                phone.line.connector.code,
                                phone.line.line_id,
                                media_url,
                                filename,
                                media_id=media_id,
                                message_id=message_id,
                                push_name=user_name,
                            )
                    except Exception as e:
                        logger.warning(f"Failed to queue voice transcription for message {message_id}: {e}")

        statuses = value.get("statuses", [])
        if statuses:
            for item in statuses:
                fb_status = item.get("status")
                wamid = item.get("id")
                out_message = None
                broadcast_recipient = None

                if wamid and fb_status:
                    try:
                        broadcast_recipient = TemplateBroadcastRecipient.objects.filter(wamid=wamid).first()
                        if broadcast_recipient:
                            previous_status = broadcast_recipient.status
                            if previous_status != fb_status:
                                update_fields = {"status": fb_status}
                                if fb_status == "failed":
                                    update_fields["error_json"] = item
                                TemplateBroadcastRecipient.objects.filter(id=broadcast_recipient.id).update(**update_fields)
                                if fb_status == "delivered" and previous_status != "delivered":
                                    TemplateBroadcast.objects.filter(id=broadcast_recipient.broadcast_id).update(
                                        delivered_count=models.F("delivered_count") + 1
                                    )
                    except Exception:
                        pass

                if broadcast_recipient:
                    continue

                if fb_status == "failed":
                    fallback_triggered = False
                    try:
                        error_data = extract_error_data(item)
                        error_obj = save_error_data(error_data)
                        if error_obj and error_obj.fallback:
                            saved_text = redis_client.get(f"wamid:{wamid}")
                            if saved_text:
                                saved_text = saved_text.decode('utf-8') if isinstance(saved_text, bytes) else saved_text
                                saved_text = _sanitize_template_param_text(saved_text)
                                default_template = Template.objects.filter(waba=phone.waba, default=True).first()
                                if default_template and saved_text:
                                    user_identy = item.get("recipient_id")
                                    body_parameters = _build_fallback_body_parameters(default_template, saved_text)
                                    payload = {
                                        "messaging_product": "whatsapp",
                                        "type": "template",
                                        "to": user_identy,
                                        "template": {
                                            "name": default_template.name,
                                            "language": {"code": default_template.lang},
                                            "components": [
                                                {
                                                    "type": "body",
                                                    "parameters": body_parameters
                                                }
                                            ]
                                        }
                                    }
                                    resp = call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/messages", method="post", payload=payload)
                                    fallback_triggered = True
                                    if user_identy and appinstance:
                                        if "error" in resp:
                                            msg = f"[color=#ff0000]Error occurred: {resp}[/color]"
                                        else:
                                            msg = f"[color=#00ff00]The message was sent using the default template due to error {error_code}[/color]"
                                            message_obj = Message.objects.filter(site__domain=host, code="default_template").first()
                                            if message_obj:
                                                msg = f"[color=#00ff00]{message_obj.message} {error_code}[/color]"
                                        bitrix_tasks.send_messages.delay(
                                            appinstance.id,
                                            user_identy,
                                            msg,
                                            phone.line.connector.code,
                                            phone.line.line_id,
                                            manager_id=0,
                                        )
                    except Exception:
                        pass
                        
                    if not fallback_triggered:
                        try:
                            out_message = error_message(item)
                            user_identy = item.get("recipient_id")
                            if user_identy and appinstance:
                                bitrix_tasks.send_messages.delay(
                                    appinstance.id,
                                    user_identy,
                                    f"[color=#ff0000]{out_message}[/color]",
                                    phone.line.connector.code,
                                    phone.line.line_id,
                                    manager_id=0,
                                )
                        except Exception:
                            pass

                biz_opaque_callback_data = item.get("biz_opaque_callback_data")
                if biz_opaque_callback_data:
                    try:
                        callback_data = json.loads(biz_opaque_callback_data)
                        bitrix_user_id = callback_data.get('bitrix_user_id')
                        sms_message_id = callback_data.get('sms_message_id')
                        
                        # if fb_status == "failed" and bitrix_user_id and not fallback_triggered:
                        #     if not out_message:
                        #         out_message = error_message(item)
                        #     payload = {"USER_ID": bitrix_user_id, "MESSAGE": out_message}
                        #     bitrix_tasks.call_api.delay(appinstance.id, "im.notify.system.add", payload)
                        
                        if sms_message_id and fb_status in ["failed"]:
                            status_data = {
                                "CODE": phone.line.connector.code,
                                "MESSAGE_ID": sms_message_id,
                                "STATUS": fb_status
                            }
                            bitrix_tasks.call_api(appinstance.id, "messageservice.message.status.update", status_data)
                    except Exception:
                        pass

        if text and user_identy:
            if not ctwa_enabled:
                ctwa_id = None
                source_id = None
            send_result = send_bitrix_message_from_waba(
                appinstance.id,
                user_identy,
                text,
                phone.line.connector.code,
                phone.line.line_id,
                pushName=user_name,
                message_id=message_id,
                attachments=attach,
                chat_url=source_url,
                ctwa_id=ctwa_id,
                source_id=source_id,
            )
            if message_type not in ["unsupported", "system"]:
                _queue_waba_read_status(waba, phone, message_id)
            if referral_body:
                bitrix_tasks.send_messages.delay(
                    appinstance.id,
                    user_identy,
                    referral_body,
                    phone.line.connector.code,
                    phone.line.line_id,
                    manager_id=0,
                )

    elif field == 'smb_message_echoes':
        if not appinstance or not phone.line_id or not phone.line.connector_id:
            return "WABA phone is not connected to Bitrix line"

        text = None
        attach= None
        message_echoes = value.get("message_echoes", {})
        for message in message_echoes:
            user_identy = message.get("to")
            message_type = message.get("type")
            if message_type == "text":
                text = message.get("text", {}).get("body")

            elif message_type == "button":
                text = message.get("button", {}).get("text")

            elif message_type == "contacts":
                contacts = message.get("contacts", [])
                text = format_contacts(contacts)

            elif message_type in ["image", "video", "audio", "document", "sticker"]:
                media_data = message.get(message_type)
                media_id = media_data["id"]
                media_url = media_data.get("url")
                if not media_url:
                    try:
                        media_info = call_api(waba=phone.waba, endpoint=media_id)
                        media_url = media_info.get("url")
                    except Exception:
                        pass

                original_filename = media_data.get("filename")
                extension = _resolve_media_extension(media_data.get("mime_type"), original_filename)
                filename = f"wamid.{media_id}.{extension}"
                
                text = media_data.get("caption") or ""
                if original_filename:
                    text = f"{original_filename} {text}"
                text = text.strip() if text else None

                try:
                    file_url = get_file(media_url, filename, appinstance, phone.waba, media_id=media_id, phone=phone)
                except TRANSIENT_ERRORS:
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
                            "url": file_url,
                            "name": filename,
                        }
                    ]
            
            elif message_type == "reaction":
                 reaction = message.get("reaction")
                 text = reaction.get("emoji")

            elif message_type == "edit":
                edit_data = message.get("edit", {})
                edited_message = edit_data.get("message", {})
                edited_type = edited_message.get("type")
                if edited_type == "text":
                    edited_text = (edited_message.get("text") or {}).get("body")
                    text = f"📝: {edited_text}" if edited_text else "Edited:"
                else:
                    raise Exception(f"Unsupported edit.message type: {edited_type}")

            elif message_type == "unsupported":
                text = f"[color=#ff0000]{error_message(message)}[/color]"

            else:
                raise Exception(f"Unsupported smb_message_echoes message_type: {message_type}")

            if text or attach:
                return send_bitrix_message_from_waba(
                    appinstance.id,
                    user_identy,
                    text,
                    phone.line.connector.code,
                    phone.line.line_id,
                    attachments=attach,
                    manager_id=0,
                )
    else:
        raise Exception(f"this event is not handled")

    return send_result


@shared_task(queue='waba', **RETRY_KWARGS)
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
            category = template.get("category")

            fetch_and_save_template(
                waba,
                template_id,
                name,
                lang,
                event_status=status,
                components=content,
                category=category,
            )
    except Exception:
        raise

import base64
import json
import logging
import re
import uuid
import os
import redis
import requests
from django.core import signing
from django.core.signing import TimestampSigner
from urllib.parse import unquote
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.contrib import messages
from django.conf import settings
from django.shortcuts import get_object_or_404

from celery import shared_task

import separator.olx.tasks as olx_tasks
import separator.waba.utils as waba
import separator.waba.tasks as waba_tasks
from separator.waba.models import Ctwa, Phone

from separator.waweb.models import Session
import separator.waweb.tasks as waweb_tasks

from .models import App, AppInstance, Bitrix, Line, VerificationCode, Connector, Credential, Events
from .models import User as B24_user
from .retry import RETRY_KWARGS, TRANSIENT_ERRORS

import separator.bitrix.tasks as bitrix_tasks
import separator.bitbot.router as bitbot_router

if settings.ASTERX_SERVER:
    from separator.asterx.models import Server
    from separator.asterx.utils import send_call_info

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)

WABA_IMAGE_MAX_SIZE = 5 * 1024 * 1024
WABA_MEDIA_MAX_SIZE = 16 * 1024 * 1024
WABA_DOCUMENT_MAX_SIZE = 100 * 1024 * 1024

WABA_IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}
WABA_AUDIO_MIME_TYPES = {"audio/aac", "audio/amr", "audio/mpeg", "audio/mp4"}
WABA_VIDEO_MIME_TYPES = {"video/3gpp", "video/mp4"}
WABA_STREAM_UPLOAD_ATTEMPTS = 3


def _normalize_phone_value(value):
    if not value:
        return []
    digits = re.sub(r"\D", "", str(value))
    return [f"+{digits}", digits] if digits else []


def _parse_file_size(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_waba_file_type(file):
    mime_type = (file.get("mime") or "").split(";", 1)[0].strip().lower()
    file_size = _parse_file_size(file.get("size"))

    if file_size is not None and file_size > WABA_DOCUMENT_MAX_SIZE:
        return "link"

    if mime_type in WABA_IMAGE_MIME_TYPES and (file_size is None or file_size <= WABA_IMAGE_MAX_SIZE):
        return "image"

    if mime_type in WABA_AUDIO_MIME_TYPES and (file_size is None or file_size <= WABA_MEDIA_MAX_SIZE):
        return "audio"

    if mime_type in WABA_VIDEO_MIME_TYPES and (file_size is None or file_size <= WABA_MEDIA_MAX_SIZE):
        return "video"

    return "document"


def _upload_waba_file_with_retries(phone, file):
    last_result = None
    for attempt in range(1, WABA_STREAM_UPLOAD_ATTEMPTS + 1):
        logger.info(
            "B24->WABA file streaming upload attempt %s/%s: %s %s",
            attempt,
            WABA_STREAM_UPLOAD_ATTEMPTS,
            file.get("name"),
            file.get("link"),
        )
        last_result = waba.upload_media_url_for_phone(
            phone,
            file["link"],
            file["name"],
            source_url=file["link"],
            mime_type=file.get("mime"),
        )
        if last_result and "id" in last_result:
            return last_result
    return last_result


def _find_ctwa_for_bizproc(appinstance, ctwa_id=None, client_phone=None):
    # CTWA ID has priority; phone lookup is a fallback for robots that pass only a client phone.
    ctwa_id = str(ctwa_id or "").strip()
    if ctwa_id:
        ctwa = Ctwa.objects.filter(id=ctwa_id).first()
        if ctwa:
            return ctwa

    phones = _normalize_phone_value(client_phone)
    if not phones:
        return None

    ctwa_qs = Ctwa.objects.filter(phone__in=phones)
    if appinstance.portal_id:
        ctwa_qs = ctwa_qs.filter(
            Q(waba_phone__line__portal=appinstance.portal)
            | Q(waba__phones__line__portal=appinstance.portal)
        )
    return ctwa_qs.distinct().first()


def format_waba_error(send_result):
    if not isinstance(send_result, dict) or "error" not in send_result:
        return ""

    error_msg = send_result.get("message", "Unknown error")
    error_data = waba.extract_error_data(send_result)
    if error_data:
        return waba.error_message({"errors": [error_data], "recipient_id": "Error"})

    return str(error_msg)


def send_error_to_openline(app_instance_id, user_phone, error_msg, connector_code, line_id):
    bitrix_tasks.send_messages.delay(
        app_instance_id,
        user_phone,
        f"[color=#ff0000]{error_msg}[/color]",
        connector_code,
        line_id,
        manager_id=0,
    )


def send_waba_error_to_openline(app_instance_id, user_phone, error_result, connector_code, line_id):
    send_error_to_openline(
        app_instance_id,
        user_phone,
        format_waba_error(error_result),
        connector_code,
        line_id,
    )


def raise_waba_send_error(send_result):
    if not waba.is_retry_enabled_for_error(send_result):
        raise waba.WabaNonRetryableError(send_result)
    raise requests.RequestException(send_result)


logger = logging.getLogger("django")

def get_app(auth_id):
    try:        
        response = requests.get(f"{settings.BITRIX_OAUTH_URL}/rest/app.info", params={"auth": auth_id})
        response.raise_for_status()
        app_data = response.json().get("result")
        client_id = app_data.get("client_id")
    except requests.RequestException:
        raise
    
    try:
        app = App.objects.get(client_id=client_id)
    except Exception as e:
        raise
    try:
        install = app_data.get("install", {}) if isinstance(app_data, dict) else {}
        server_version = install.get("version")
        server_version = int(server_version) if server_version is not None else None
    except Exception:
        server_version = None

    try:
        if not app.autologin:
            app.autologin = False
        elif app.min_version == 0:
            app.autologin = True
        elif server_version is not None and server_version > app.min_version:
            app.autologin = False
    except Exception:
        pass

    return app


def get_instances(request, service=None):
    b24_users = B24_user.objects.filter(
        owner=request.user,
        admin=True,
        active=True,
        bitrix__isnull=False,
    )
    portal_ids = b24_users.values_list('bitrix_id', flat=True).distinct()
    portals = Bitrix.objects.filter(id__in=portal_ids).distinct()
    lines = Line.objects.filter(portal__in=portals).distinct()
    if service:
        instances = AppInstance.objects.filter(portal__in=portals, app__connectors__service=service).distinct()
        lines = lines \
        .exclude(phones__isnull=False) \
        .exclude(wawebs__isnull=False) \
        .exclude(olx_users__isnull=False)
        return portals, instances, lines
    else:
        return portals, lines


def get_line_app_instance(line: Line, connector_service=None):
    if not line or not line.portal_id:
        return None

    instances = AppInstance.objects.filter(portal=line.portal).select_related("app", "portal")
    if connector_service:
        instances = instances.filter(app__connectors__service=connector_service)
    elif line.connector_id:
        instances = instances.filter(app__connectors=line.connector)
    else:
        instances = instances.filter(app__connectors__isnull=False)
    instances = instances.distinct()
    if connector_service:
        return instances.get()
    return instances.order_by("id").first()


def sync_portal_open_lines(appinstance: AppInstance):
    if not appinstance or not appinstance.portal_id:
        return 0

    synced = 0
    limit = 50
    offset = 0

    try:
        while True:
            response = bitrix_tasks.call_api(appinstance.id, "imopenlines.config.list.get", {
                "PARAMS": {
                    "select": ["ID", "LINE_NAME"],
                    "order": {"ID": "ASC"},
                    "limit": limit,
                    "offset": offset,
                }
            })
            configs = response.get("result") if isinstance(response, dict) else response
            if not isinstance(configs, list) or not configs:
                break

            for config in configs:
                if not isinstance(config, dict):
                    continue
                bitrix_line_id = str(config.get("ID") or config.get("id") or "").strip()
                if not bitrix_line_id:
                    continue

                name = config.get("LINE_NAME") or config.get("line_name") or config.get("NAME") or "openline"
                line = Line.objects.filter(
                    line_id=bitrix_line_id,
                    portal=appinstance.portal,
                ).order_by("id").first()
                if line:
                    if name and line.name != name:
                        line.name = name
                        line.save(update_fields=["name"])
                else:
                    Line.objects.create(
                        line_id=bitrix_line_id,
                        portal=appinstance.portal,
                        name=name,
                    )
                synced += 1

            if len(configs) < limit:
                break
            offset += limit
    except Exception:
        logger.warning("Failed to sync open lines for AppInstance %s", appinstance.id, exc_info=True)

    return synced


def get_b24_user(app: App, portal: Bitrix, auth_id, refresh_id=None):
    try:
        profile = requests.post(f"{portal.protocol}://{portal.domain}/rest/profile", json={"auth": auth_id}, timeout=10)
        profile_data = profile.json().get("result")
        admin = profile_data.get("ADMIN")
        user_id = profile_data.get("ID")
    except Exception as e:
        raise Exception(f"Ошибка: {e}")
    
    b24_user, user_created = B24_user.objects.get_or_create(
        bitrix=portal,
        user_id=user_id,
        defaults={
            "admin": admin
        }
    )

    if not user_created:
        b24_user.admin = admin
        b24_user.active = True
        b24_user.save()

    app_instance = AppInstance.objects.filter(portal=portal, app=app).first()
    if app_instance:
        cred, created = Credential.objects.get_or_create(
            app_instance=app_instance,
            user=b24_user,
            defaults={
                "access_token": auth_id,
                "refresh_token": refresh_id or " ",
            }
        )
        if not created:
            cred.access_token = auth_id
            update_fields = ["access_token", "refresh_date"]
            if refresh_id is not None:
                cred.refresh_token = refresh_id
                update_fields.append("refresh_token")
            cred.refresh_date = timezone.now()
            cred.save(update_fields=["access_token", "refresh_token", "refresh_date"])
    return b24_user


def connect_line(request, line_id, entity, connector_service):
    if not line_id:
        messages.warning(request, "Необходимо выбрать линию из списка или создать новую.")
        return
    line_id = str(line_id)
    if line_id.startswith("create__"):
        instance_id = line_id.split("__")[1]
        app_instance = get_object_or_404(AppInstance, id=instance_id)
        connector = app_instance.app.connectors.filter(service=connector_service).first()
        if not connector:
            messages.error(request, "Не найден коннектор для установки приложения.")
            return
        if entity.line:
            old_app_instance = get_line_app_instance(entity.line, connector_service) or app_instance
            bitrix_tasks.call_api(old_app_instance.id, "imconnector.activate", {
                "CONNECTOR": connector.code,
                "LINE": entity.line.line_id,
                "ACTIVE": 0,
            })
        if connector.service == "olx":
            line_name = entity.olx_id
        else:
            line_name = entity.phone

        params = {}

        if connector.default_line_params and isinstance(connector.default_line_params, dict):
            params.update(connector.default_line_params)

        params["LINE_NAME"] = line_name
        params["ACTIVE"] = "Y"

        create_payload = {
            "PARAMS": params
        }
        result = bitrix_tasks.call_api(app_instance.id, "imopenlines.config.add", create_payload)
        if result and result.get("result"):
            new_line_id = result["result"]
            line = Line.objects.create(
                line_id=new_line_id,
                portal=app_instance.portal,
                connector=connector,
                name=line_name,
            )
            entity.line = line
            if any(field.name == "app_instance" for field in entity._meta.fields):
                entity.app_instance = app_instance
            entity.save()

            activate_payload = {
                "CONNECTOR": connector.code,
                "LINE": new_line_id,
                "ACTIVE": 1,
            }
            bitrix_tasks.call_api(app_instance.id, "imconnector.activate", activate_payload)
            bitrix_tasks.messageservice_add.delay(app_instance.id, entity.id, connector.service)
            messages.success(request, f"Создана и подключена линия {new_line_id}")
        else:
            messages.error(request, f"Ошибка при создании линии: {result}")
            return
    else:
        line = get_object_or_404(Line, id=line_id)                
        app_instance = get_line_app_instance(line, connector_service)
        if not app_instance:
            messages.error(request, "Не найдена установка приложения для портала линии.")
            return
        connector = app_instance.app.connectors.filter(service=connector_service).first()
        if not connector:
            messages.error(request, "Не найден коннектор для установки приложения.")
            return
        bitrix_tasks.messageservice_add.delay(app_instance.id, entity.id, connector.service)       
        if entity.line:
            if str(entity.line.id) == str(line_id):
                messages.warning(request, "Эта линия уже используется.")
                return
            old_app_instance = get_line_app_instance(entity.line, connector_service) or app_instance
            bitrix_tasks.call_api(old_app_instance.id, "imconnector.activate", {
                "CONNECTOR": connector.code,
                "LINE": entity.line.line_id,
                "ACTIVE": 0,
            })
        response = bitrix_tasks.call_api(app_instance.id, "imconnector.activate", {
            "CONNECTOR": connector.code,
            "LINE": line.line_id,
            "ACTIVE": 1,
        })
        if response.get("result"):
            if line.connector_id != connector.id:
                line.connector = connector
                line.save(update_fields=["connector"])
            entity.line = line
            if any(field.name == "app_instance" for field in entity._meta.fields):
                entity.app_instance = app_instance
            entity.save()
            messages.success(request, "Линия подключена")


# Подписка на события
def events_bind(appinstance: AppInstance):
    handler_url = appinstance.app.get_bitrix_handler_url() if appinstance.app else ""
    if not handler_url:
        return
    try:
        existing = bitrix_tasks.call_api(appinstance.id, "event.get", {})
    except Exception:
        existing = {}

    handlers = existing.get("result")
    if handlers is None:
        handlers = existing if isinstance(existing, list) else []

    for event in appinstance.app.events.strip().splitlines():
        event = event.strip()
        if not event:
            continue

        matched_handlers = [h for h in handlers if h.get("event") == event]
        has_current = any(h.get("handler") == handler_url for h in matched_handlers)
        for h in matched_handlers:
            if h.get("handler") and h.get("handler") != handler_url:
                bitrix_tasks.call_api.delay(
                    appinstance.id,
                    "event.unbind",
                    {"event": event, "HANDLER": h.get("handler")},
                )

        if not has_current:
            payload = {
                "event": event,
                "HANDLER": handler_url,
            }
            bitrix_tasks.call_api.delay(appinstance.id, "event.bind", payload)


def register_connector(appinstance: AppInstance, connector):
    url = appinstance.app.site

    if not connector.icon:
        return None

    try:
        with open(connector.icon.path, "rb") as file:
            image_data = file.read()
            encoded_image = base64.b64encode(image_data).decode("utf-8")
            connector_logo = f"data:image/svg+xml;base64,{encoded_image}"

        payload = {
            "ID": connector.code,
            "NAME": connector.name,
            "ICON": {
                "DATA_IMAGE": connector_logo,
            },
            "PLACEMENT_HANDLER": f"https://{url}/placement/?inst={appinstance.id}",
        }

        bitrix_tasks.call_api.delay(appinstance.id, "imconnector.register", payload)

    except FileNotFoundError:
        return None
    except Exception as e:
        return None


def queue_app_features(appinstance: AppInstance):
    app = appinstance.app
    if not app:
        return

    for feature in app.features.filter(active=True):
        placement_codes = [p.strip() for p in (feature.placements or "").splitlines() if p.strip()]
        if placement_codes:
            for placement_code in placement_codes:
                bitrix_tasks.register_feature.delay(appinstance.id, feature.id, placement_code=placement_code)
            continue
        bitrix_tasks.register_feature.delay(appinstance.id, feature.id)


def extract_files(data):
    files = []
    i = 0
    while True:
        # Формируем ключи для доступа к данным файлов
        name_key = f"data[MESSAGES][0][message][files][{i}][name]"
        link_key = f"data[MESSAGES][0][message][files][{i}][link]"
        download_link_key = f"data[MESSAGES][0][message][files][{i}][downloadLink]"
        type_key = f"data[MESSAGES][0][message][files][{i}][type]"
        mime_key = f"data[MESSAGES][0][message][files][{i}][mime]"
        size_key = f"data[MESSAGES][0][message][files][{i}][size]"
        sizef_key = f"data[MESSAGES][0][message][files][{i}][sizef]"

        # Проверяем, существуют ли такие ключи в словаре
        if name_key in data and (download_link_key in data or link_key in data):
            # Добавляем название и ссылку в список
            files.append(
                {
                    "name": data.get(name_key),
                    "link": data.get(download_link_key) or data.get(link_key),
                    "download_link": data.get(download_link_key),
                    "source_link": data.get(link_key),
                    "type": data.get(type_key),
                    "mime": data.get(mime_key),
                    "size": data.get(size_key),
                    "sizef": data.get(sizef_key),
                },
            )
            i += 1
        else:
            break

    return files


def upload_file(appinstance, storage_id, fileContent, filename):
    payload = {
        "id": storage_id,
        "fileContent": fileContent,
         "data": {"NAME": filename},
         "generateUniqueName": True,
    }
    upload_to_bitrix = bitrix_tasks.call_api(appinstance.id, "disk.storage.uploadfile", payload)
    if "result" in upload_to_bitrix:
        return upload_to_bitrix["result"]
    else:
        return None


def build_waba_media_proxy_url(app_instance, phone, media_id, filename):
    if not app_instance or not phone or not media_id:
        return None

    payload = {
        "media_id": media_id,
        "phone_id": phone.id,
        "filename": filename or "file",
    }
    token = signing.dumps(payload, salt="waba-media-proxy")

    base_url = getattr(settings, "BITRIX_TEMP_FILE_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}/media/waba/?{token}"

    domain = getattr(app_instance, "host", None)
    if not domain and app_instance.app and app_instance.app.site:
        domain = app_instance.app.site.domain
    if not domain:
        return None
    domain = domain.replace("http://", "").replace("https://", "").strip("/")
    return f"https://{domain}/media/waba/?{token}"



def _get_hashtag_interactive(text, appinstance):
    hashtag = (text or "").strip()
    if not hashtag or not appinstance or not appinstance.portal_id:
        return None
    from separator.waba.models import Interactive
    return (
        Interactive.objects
        .filter(hashtag=hashtag)
        .filter(Q(portal=appinstance.portal) | Q(**{"global": True}))
        .order_by("-global", "name")
        .first()
    )


def _build_file_header_component(file_url, appinstance=None, line_id=None, phone_num=None):
    if not file_url:
        return None

    upload_phone = None
    if appinstance and phone_num:
        upload_phone = Phone.objects.filter(phone=f"+{phone_num}").first()
    elif appinstance and line_id:
        line = Line.objects.filter(line_id=line_id, portal=appinstance.portal).first()
        upload_phone = Phone.objects.filter(line=line).first() if line else None

    if upload_phone:
        # Check cache before HEAD/GET requests to the original file host.
        uploaded_id = waba.get_cached_media_id_for_phone(upload_phone, file_url)
        if uploaded_id:
            lower_url = file_url.split("?", 1)[0].lower()
            if lower_url.endswith((".jpg", ".jpeg", ".png", ".webp")):
                waba_file_type = "image"
            elif lower_url.endswith((".mp4", ".mov", ".avi", ".webm")):
                waba_file_type = "video"
            elif lower_url.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")):
                waba_file_type = "document"
            else:
                waba_file_type = None

            if waba_file_type:
                media_payload = {"id": uploaded_id}
                if waba_file_type == "document":
                    media_payload["filename"] = os.path.basename(file_url.split("?", 1)[0]) or "file"
                return {
                    "type": "header",
                    "parameters": [{"type": waba_file_type, waba_file_type: media_payload}],
                }

    file_type = ""
    file_headers = None
    try:
        file_headers = requests.head(file_url, allow_redirects=True, timeout=10)
        file_type = file_headers.headers.get("Content-Type", "")
    except Exception:
        file_type = ""

    if file_type.startswith("image/"):
        waba_file_type = "image"
    elif file_type.startswith("video/"):
        waba_file_type = "video"
    elif file_type == "application/pdf" or file_url.lower().endswith(".pdf"):
        waba_file_type = "document"
    else:
        waba_file_type = "document"

    uploaded_id = None
    filename = "file"
    if waba_file_type == "document":
        filename = "file.pdf"
        if file_headers:
            cd = file_headers.headers.get("Content-Disposition", "")
            m = re.search(r"filename\*=utf-8''(.+)", cd)
            if m:
                filename = unquote(m.group(1))
            else:
                m = re.search(r'filename="(.+?)"', cd)
                if m:
                    filename = m.group(1)
    else:
        ext = file_type.split("/")[-1] if file_type and "/" in file_type else "bin"
        filename = f"file.{ext}"

    if appinstance and (line_id or phone_num):
        try:
            r = requests.get(file_url, timeout=30)
            if r.status_code == 200:
                up_res = waba.upload_media(
                    appinstance,
                    r.content,
                    file_type,
                    filename,
                    line_id=line_id,
                    phone_num=phone_num,
                    source_url=file_url,
                )
                if up_res and "id" in up_res:
                    uploaded_id = up_res["id"]
        except Exception as e:
            logger.error(f"Template media upload failed: {e}")

    file_param = {"type": waba_file_type}
    if uploaded_id:
        file_param[waba_file_type] = {"id": uploaded_id}
    else:
        file_param[waba_file_type] = {"link": file_url}

    if waba_file_type == "document":
        file_param[waba_file_type]["filename"] = filename

    return {
        "type": "header",
        "parameters": [file_param],
    }


def _parse_shortcode_params(payload: str) -> dict:
    params = {}
    position = 1
    for segment in (payload or "").split("|"):
        item = segment.strip()
        if not item:
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.*)$", item, re.S)
        if match:
            params[match.group(1)] = match.group(2).strip() or "-"
        else:
            params[f"param{position}"] = item
            position += 1
    return params


def _render_interactive_value(value, params):
    if isinstance(value, str):
        return re.sub(
            r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}",
            lambda match: str(params.get(match.group(1), "-")),
            value,
        )
    if isinstance(value, list):
        return [_render_interactive_value(item, params) for item in value]
    if isinstance(value, dict):
        return {
            key: _render_interactive_value(item, params)
            for key, item in value.items()
        }
    return value


def _build_interactive_header(header, phone=None):
    if not header:
        return None
    header_type = header.get("type")
    value = header.get("value")
    if not header_type or not value:
        return None
    if header_type == "text":
        return {"type": "text", "text": value}
    if header_type in {"image", "video", "document"}:
        if phone:
            # Check cache before downloading media from the customer-provided source.
            media_id = waba.get_cached_media_id_for_phone(phone, value)
            if media_id:
                return {"type": header_type, header_type: {"id": media_id}}
            try:
                response = requests.get(value, timeout=(10, 60))
                if response.status_code == 200:
                    filename = os.path.basename(value.split("?", 1)[0]) or "file"
                    upload = waba.upload_media_for_phone(
                        phone,
                        response.content,
                        response.headers.get("Content-Type", "application/octet-stream"),
                        filename,
                        source_url=value,
                    )
                    media_id = upload.get("id") if isinstance(upload, dict) else None
                    if media_id:
                        return {"type": header_type, header_type: {"id": media_id}}
            except Exception as e:
                logger.error(f"Interactive media upload failed: {e}")
        return {"type": header_type, header_type: {"link": value}}
    return None


def parse_interactive_code(code: str, appinstance=None, phone=None) -> dict:
    parts = code.split("+", 2)
    if len(parts) < 2 or parts[0] != "interactive":
        raise ValueError("Invalid interactive code")

    try:
        message_id = uuid.UUID(parts[1].strip())
    except Exception:
        raise ValueError("Invalid interactive message ID")

    from separator.waba.models import Interactive

    interactive_message = Interactive.objects.filter(id=message_id).first()
    if not interactive_message:
        raise ValueError("Interactive message not found")

    params = _parse_shortcode_params(parts[2] if len(parts) > 2 else "")
    payload = _render_interactive_value(interactive_message.payload or {}, params)
    interactive_type = interactive_message.type

    interactive = {"type": interactive_type}
    header = _build_interactive_header(payload.get("header"), phone=phone)
    if header and interactive_type not in {"voice_call", "call_permission_request"}:
        interactive["header"] = header
    if payload.get("body"):
        interactive["body"] = {"text": payload["body"]}
    if payload.get("footer") and interactive_type not in {"voice_call", "call_permission_request"}:
        interactive["footer"] = {"text": payload["footer"]}

    if interactive_type == "button":
        buttons = []
        for button in payload.get("buttons", []):
            buttons.append({
                "type": "reply",
                "reply": {
                    "id": button.get("id"),
                    "title": button.get("title"),
                },
            })
        interactive["action"] = {"buttons": buttons}

    elif interactive_type == "list":
        interactive["action"] = {
            "button": payload.get("button"),
            "sections": payload.get("sections", []),
        }

    elif interactive_type == "cta_url":
        interactive["action"] = {
            "name": "cta_url",
            "parameters": {
                "display_text": payload.get("display_text"),
                "url": payload.get("url"),
            },
        }

    elif interactive_type == "voice_call":
        parameters = {}
        if payload.get("display_text"):
            parameters["display_text"] = payload.get("display_text")
        if payload.get("ttl_minutes"):
            parameters["ttl_minutes"] = payload.get("ttl_minutes")
        if payload.get("call_payload"):
            parameters["payload"] = payload.get("call_payload")
        interactive["action"] = {"name": "voice_call"}
        if parameters:
            interactive["action"]["parameters"] = parameters

    elif interactive_type == "call_permission_request":
        interactive["action"] = {"name": "call_permission_request"}

    else:
        raise ValueError("Unsupported interactive message type")

    return {
        "type": "interactive",
        "interactive": interactive,
    }


def parse_template_code(code: str, appinstance=None, line_id=None, phone_num=None) -> dict:
    try:
        # New shortcode format: template+<template_id>+param1:value1|param2:value2
        # Lookup template in DB by id and build parameters against saved template schema.
        new_parts = code.split("+", 2)
        if len(new_parts) >= 2 and new_parts[0] == "template":
            template_id = new_parts[1].strip()
            if template_id:
                from separator.waba.models import Template  # local import to avoid circular import

                template = (
                    Template.objects.filter(id=template_id)
                    .prefetch_related(
                        "components__named_params",
                        "components__positional_params",
                        "components__buttons__named_params",
                        "components__buttons__positional_params",
                    )
                    .first()
                )
                if template:
                    payload = new_parts[2] if len(new_parts) > 2 else ""

                    expected_names = set()
                    for comp in template.components.all():
                        for p in comp.named_params.all():
                            expected_names.add(p.name.strip())
                            
                    search_names = expected_names | {"button_param", "file_link"}
                    
                    shortcode_pairs = {}
                    positional_values = []
                    button_param = None
                    file_url = None

                    raw_segments = payload.split("|") if payload else []
                    for segment in raw_segments:
                        item = segment.strip()
                        if not item:
                            continue

                        matched_name = None
                        for name in search_names:
                            token = name + ":"
                            if item.startswith(token):
                                matched_name = name
                                value = item[len(token):].strip()
                                break

                        if not matched_name:
                            positional_values.append(item)
                            continue

                        if matched_name == "button_param":
                            button_param = value or "-"
                        elif matched_name == "file_link":
                            file_url = value
                        else:
                            shortcode_pairs[matched_name] = value or "-"

                    def _value_for_named(name):
                        value = shortcode_pairs.get(name)
                        return value if value else "-"

                    def _value_for_pos(position):
                        for key in (str(position), f"param{position}", f"p{position}"):
                            value = shortcode_pairs.get(key)
                            if value:
                                return value
                        if positional_values:
                            return positional_values.pop(0)
                        return "-"

                    message = {
                        "type": "template",
                        "template": {
                            "name": template.name,
                            "language": {"code": template.lang},
                        },
                    }
                    components = []

                    for component in template.components.order_by("index", "id"):
                        comp_type = (component.type or "").upper()
                        comp_format = (component.format or "").upper()

                        if comp_type == "HEADER" and comp_format == "TEXT":
                            header_params = []
                            for p in component.named_params.order_by("id"):
                                header_params.append(
                                    {
                                        "type": "text",
                                        "parameter_name": p.name,
                                        "text": _value_for_named(p.name),
                                    }
                                )
                            for p in component.positional_params.order_by("position", "id"):
                                header_params.append(
                                    {
                                        "type": "text",
                                        "text": _value_for_pos(p.position),
                                    }
                                )
                            if header_params:
                                components.append({"type": "header", "parameters": header_params})

                        if comp_type == "BODY":
                            body_params = []
                            for p in component.named_params.order_by("id"):
                                body_params.append(
                                    {
                                        "type": "text",
                                        "parameter_name": p.name,
                                        "text": _value_for_named(p.name),
                                    }
                                )
                            for p in component.positional_params.order_by("position", "id"):
                                body_params.append(
                                    {
                                        "type": "text",
                                        "text": _value_for_pos(p.position),
                                    }
                                )
                            if body_params:
                                components.append({"type": "body", "parameters": body_params})

                        if comp_type == "BUTTONS":
                            for button in component.buttons.order_by("index", "id"):
                                if (button.type or "").upper() != "URL":
                                    continue

                                # Static URL buttons do not require parameters and WhatsApp API will reject them
                                has_params = len(button.named_params.all()) > 0 or len(button.positional_params.all()) > 0
                                if not has_params:
                                    break

                                button_value = button_param
                                if not button_value:
                                    named_button = button.named_params.all()[0] if button.named_params.all() else None
                                    if named_button:
                                        button_value = _value_for_named(named_button.name)
                                    else:
                                        positional_button = button.positional_params.all()[0] if button.positional_params.all() else None
                                        if positional_button:
                                            button_value = _value_for_pos(positional_button.position)
                                        else:
                                            button_value = "-"

                                components.append(
                                    {
                                        "type": "button",
                                        "sub_type": "url",
                                        "index": str(button.index or 0),
                                        "parameters": [{"type": "text", "text": button_value or "-"}],
                                    }
                                )
                                break

                    file_component = _build_file_header_component(
                        file_url,
                        appinstance=appinstance,
                        line_id=line_id,
                        phone_num=phone_num,
                    )
                    if file_component:
                        components.insert(0, file_component)

                    if components:
                        message["template"]["components"] = components
                    return message

        # LEGACY shortcode format: template+<template_name>+<lang>+<payload>
        # Keep existing behavior unchanged for backward compatibility.
        parts = code.split("+", 3)
        if len(parts) < 3:
            raise ValueError("Invalid message body format")

        _, template_name, language = parts[:3]
        payload = parts[3] if len(parts) > 3 else ""

        params = []
        file_url = None
        button_param = None

        # Normalize separators for file_link and button_param to |
        # This handles cases like: param1|param2+file_link:http...
        # The + before file_link becomes |
        payload = re.sub(r'\s*[+|]\s*(?=(file_link:|button_param:))', '|', payload)

        # Handle case where file_link is followed by + and text (legacy format)
        # e.g. file_link:http://url + text -> file_link:http://url|text
        # We assume URLs don't contain spaces.
        payload = re.sub(r'(file_link:[^+\s]+)\s*\+\s*', r'\1|', payload)

        # Handle case where button_param is followed by + and text (legacy format)
        # e.g. button_param:code=123+text -> button_param:code=123|text
        # We assume button params don't contain spaces or + as part of the value in this context.
        # Use simple greedy match until + since valid button params are usually alphanumeric/symbols without +.
        payload = re.sub(r'(button_param:[^+\s]+)\s*\+\s*', r'\1|', payload)

        # Split by | to get all segments, keeping empty ones to preserve parameter count
        if not payload:
            raw_segments = []
        else:
            raw_segments = payload.split('|')
        segments = []
        for s in raw_segments:
            s_stripped = s.strip()
            # If segment is empty, use a placeholder to avoid "parameter count mismatch" error.
            if not s_stripped:
                segments.append("-")
            else:
                segments.append(s_stripped)

        for p in segments:
            if p.startswith('file_link:'):
                file_url = p[len('file_link:'):]
            elif p.startswith('button_param:'):
                button_param = p[len('button_param:'):]
            else:
                # Regular numbered parameter. Clean it for WhatsApp API constraints.
                # Remove newlines/tabs and limit spaces to max 3 consecutive.
                p = re.sub(r'[\r\n\t]+', ' ', p)
                p = re.sub(r'\s{4,}', '   ', p)
                params.append(p)

        message = {
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }
        components = []
        file_component = _build_file_header_component(
            file_url,
            appinstance=appinstance,
            line_id=line_id,
            phone_num=phone_num,
        )
        if file_component:
            components.append(file_component)
        body_parameters = []
        for p in params:
            body_parameters.append({"type": "text", "text": p})
        if body_parameters:
            components.append({
                "type": "body",
                "parameters": body_parameters
            })
        if button_param:
            components.append({
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [
                    {"type": "text", "text": button_param}
                ]
            })
        if components:
            message["template"]["components"] = components
        return message
    except ValueError as e:
        raise ValueError(f"Invalid template code {code}: {e}")


def parse_block_command(command: str, phone, chat, appinstance_id=None, chat_id=None) -> dict:
    if not phone or not phone.waba or not phone.phone_id:
        raise Exception("phone not found")

    endpoint = f"{phone.phone_id}/block_users"
    payload = {
        "messaging_product": "whatsapp",
        "block_users": [
            {
                "user": re.sub(r'\D', '', str(chat or "")),
            }
        ],
    }
    method = "post" if command == "#wa_block" else "delete"
    result = waba.call_api(waba=phone.waba, endpoint=endpoint, method=method, payload=payload)
    if appinstance_id and chat_id:
        bitrix_tasks.call_api.delay(appinstance_id, "im.message.add", {
            "DIALOG_ID": f"chat{chat_id}",
            "MESSAGE": str(result),
            "SYSTEM": "Y",
        })
    return result


def send_delivery_status(appinstance_id, connector_code, line_id, chat_id, message_id):
    status_data = {
        "CONNECTOR": connector_code,
        "LINE": line_id,
        "MESSAGES": [
            {
                "im": {
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            },
        ],
    }
    bitrix_tasks.call_api.delay(appinstance_id, "imconnector.send.status.delivery", status_data)


@shared_task(queue='bitrix', **RETRY_KWARGS)
def bizproc_processor(data):
    try:
        access_token = data.get("auth[access_token]")
        application_token = data.get("auth[application_token]")
        try:
            app = get_app(access_token)
        except requests.RequestException:
            raise
        except Exception as e:
            raise Exception(f"App not found: {e}")
        try:
            appinstance = AppInstance.objects.filter(application_token=application_token).first()
        except TRANSIENT_ERRORS:
            raise
        except Exception as e:
            raise Exception(f"AppInstance not found for token {application_token}: {e}")

        if not appinstance:
            raise Exception(f"AppInstance not found for token {application_token}")
        
        if appinstance and appinstance.app and appinstance.app.save_events:
            Events.objects.create(
                app=appinstance.app,
                portal=appinstance.portal,
                content=json.dumps(data, ensure_ascii=False, default=str),
            )
        
        code = str(data.get("code") or "").strip()
        if not code:
            raise Exception("Missing bizproc code")

        grant = appinstance.get_feature_grant(code)
        if grant:
            if grant.feature and not grant.feature.active:
                return f"Feature {code} is inactive"
            if grant.date_end and grant.date_end <= timezone.now():
                return f"Feature {code} subscription expired"

        if code == "separator_auto_finish_chat":
            return bitrix_tasks.auto_finish_chat(appinstance.id, data)

        if code == "separator_ctwa_tracker":
            ctwa_id = data.get("properties[ctwa_id]")
            client_phone = data.get("properties[client_phone]")
            ctwa = _find_ctwa_for_bizproc(appinstance, ctwa_id=ctwa_id, client_phone=client_phone)
            if not ctwa:
                return "ctwa not found"

            custom_data = {}

            amount_raw = data.get("properties[amount]")
            if amount_raw:
                try:
                    custom_data["value"] = float(amount_raw)
                except ValueError:
                    pass

            currency_raw = data.get("properties[currency]")
            if currency_raw:
                custom_data["currency"] = str(currency_raw).strip()

            event_name_raw = data.get("properties[event_name]")
            event_name = str(event_name_raw).strip() if event_name_raw else ""
            if event_name:
                waba_tasks.send_ctwa_conversion.delay(
                    str(ctwa.id),
                    event=event_name,
                    custom_data=custom_data,
                )
            return

        raise Exception(f"Unhandled bizproc code {code}")
    except TRANSIENT_ERRORS:
        raise


@shared_task(bind=True, queue='bitrix', **RETRY_KWARGS)
def sms_processor(self, data, service):
    application_token = data.get("auth[application_token]")
    manager_id = data.get("auth[user_id]")
    message_id = data.get("message_id")

    def _build_waba_message(target, body, line_id=None, phone_num=None):
        message = {
            "messaging_product": "whatsapp",
            "biz_opaque_callback_data": {
                "bitrix_user_id": manager_id,
                "sms_message_id": message_id
            },
            "to": target,
        }
        if "template+" in body:
            template_start = body.index("template+")
            template_str = body[template_start:]
            message.update(parse_template_code(template_str, appinstance=app_instance, line_id=line_id))
        elif "interactive+" in body:
            interactive_start = body.index("interactive+")
            interactive_str = body[interactive_start:]
            message.update(parse_interactive_code(interactive_str, appinstance=app_instance, phone=phone))
        elif hashtag_interactive := _get_hashtag_interactive(body, app_instance):
            message.update(parse_interactive_code(f"interactive+{hashtag_interactive.id}", appinstance=app_instance, phone=phone))
        else:
            message["type"] = "text"
            message["text"] = {"body": body}
        return message

    def _send_waba_direct(target, body):
        if not phone:
            return "WABA phone not found"
        message = _build_waba_message(target, body, line_id=phone.line.line_id if phone.line_id else None)
        return waba.send_message_from_phone(phone, message)

    def _send_waba_error_to_openline(error_result):
        if service != "waba" or not line:
            return
        send_waba_error_to_openline(
            app_instance.id,
            message_to,
            error_result,
            line.connector.code,
            line.line_id,
        )

    try:
        app_instance = AppInstance.objects.filter(application_token=application_token).first()

        if app_instance and app_instance.app and app_instance.app.save_events:
            Events.objects.create(
                app=app_instance.app,
                portal=app_instance.portal,
                content=json.dumps(data, ensure_ascii=False, default=str),
            )

        if not app_instance:
            raise Exception("app not found")

        message_body = data.get("message_body")
        code = data.get("code", {})
        sender = code.split('_')[-1]
        message_to = data.get("message_to")
        line = None
        phone = None
        send_result = None

        if service == "waba":
            phone = (
                Phone.objects.select_related("line", "line__connector", "line__portal", "waba", "waba__app")
                .filter(
                    phone=f"+{sender}",
                    line__portal=app_instance.portal,
                    line__connector__service="waba",
                )
                .first()
            )
            if phone and phone.line:
                line = phone.line

        elif service == "waweb":
            wa = Session.objects.get(phone=sender)
            line = wa.line

        command = str(message_body or "").strip().lower()
        if service == "waba" and line and command in ["#wa_block", "#wa_unblock"]:
            try:
                send_result = parse_block_command(command, phone, message_to)
            except Exception as e:
                return {"error": True, "message": str(e)}
            return send_result

        if service == "waba":
            send_result = _send_waba_direct(message_to, message_body)
            if "error" in (send_result or {}):
                if self.request.retries >= self.max_retries:
                    _send_waba_error_to_openline(send_result)
                raise_waba_send_error(send_result)
            if phone and phone.ChatFromSms and line:
                bitrix_tasks.send_messages(
                    app_instance.id,
                    message_to,
                    message_body,
                    line.connector.code,
                    line.line_id,
                    manager_id=0,
                )
            return send_result

        if line and manager_id:
            send_result = bitrix_tasks.send_messages(app_instance.id, message_to, message_body,
                                                     line.connector.code, line.line_id, manager_id=manager_id)

        if "error" in (send_result or {}):
            if self.request.retries >= self.max_retries:
                _send_waba_error_to_openline(send_result)
            raise_waba_send_error(send_result)
        return send_result
    except TRANSIENT_ERRORS:
        raise
    except Exception:
        raise


@shared_task(bind=True, queue='bitrix', **RETRY_KWARGS)
def event_processor(self, data):
    try:
        event = data.get("event").upper()
        domain = data.get("auth[domain]")
        user_id = data.get("auth[user_id]")
        auth_status = data.get("auth[status]")
        scope = data.get("auth[scope]")
        access_token = data.get("auth[access_token]")
        refresh_token = data.get("auth[refresh_token]")
        application_token = data.get("auth[application_token]")
        member_id = data.get("auth[member_id]")
        scope = scope or ""
        appinstance = None

        if event == "ONAPPINSTALL":
            try:
                app = get_app(access_token)
            except Exception as e:
                raise Exception(f"App not found for token {application_token}: {e}")
            portal, _ = Bitrix.objects.get_or_create(
                member_id=member_id,
                defaults={
                    "domain": domain,
                }
            )

            if domain and portal.domain != domain:
                portal.domain = domain
                portal.save(update_fields=["domain"])

            appinstance, _ = AppInstance.objects.update_or_create(
                application_token=application_token,
                defaults={
                    "app": app,
                    "portal": portal,
                    "auth_status": auth_status,
                    "owner": portal.owner,
                }
            )

            try:
                b24_user = get_b24_user(app, portal, access_token, refresh_token)
                if portal.owner and not b24_user.owner:
                    b24_user.owner = portal.owner
                    b24_user.save(update_fields=["owner"])
            except Exception:
                pass

            if "disk" in scope:
                storage_data = bitrix_tasks.call_api(appinstance.id, "disk.storage.getforapp", {})
                if "result" in storage_data:
                    storage_id = storage_data["result"]["ID"]
                    appinstance.storage_id = storage_id
                    appinstance.save(update_fields=["storage_id"])

            def register_events_and_connectors():
                errors = []
                try:
                    sync_portal_open_lines(appinstance)
                except Exception as e:
                    errors.append(f"sync_portal_open_lines: {e}")

                try:
                    events_bind(appinstance)
                except Exception as e:
                    errors.append(f"events_bind: {e}")

                try:
                    queue_app_features(appinstance)
                except Exception as e:
                    errors.append(f"queue_app_features: {e}")
                
                if app.connectors.exists():
                    for connector in app.connectors.all():
                        try:
                            register_connector(appinstance, connector)
                        except Exception as e:
                            errors.append(f"register_connector {connector.id}: {e}")

                if errors:
                    raise Exception("; ".join(errors))

            transaction.on_commit(register_events_and_connectors)

            if settings.ASTERX_SERVER and app.asterx:
                from separator.asterx.views import get_portal_settings
                get_portal_settings(member_id)

            if portal.owner:
                return "App successfully created/updated and linked"

            verify_code = VerificationCode.objects.filter(portal=portal).first()

            if verify_code:
                code = verify_code.code
            else:
                code = uuid.uuid4()
                VerificationCode.objects.create(
                    portal=portal,
                    code=code,
                    expires_at=timezone.now() + timedelta(days=1),
                )

            payload = {
                "message": f"Для привязки портала перейдите по ссылке https://{appinstance.app.site}/portals/?code={code}",
                "USER_ID": user_id,
            }
            bitrix_tasks.call_api.delay(appinstance.id, "im.notify.system.add", payload)
        else:
            appinstance = AppInstance.objects.get(application_token=application_token)

        if appinstance and appinstance.app and appinstance.app.save_events:
            Events.objects.create(
                app=appinstance.app,
                portal=appinstance.portal,
                content=json.dumps(data, ensure_ascii=False, default=str),
            )
        
        if event == "ONIMCONNECTORMESSAGEADD":
            connector_code = data.get("data[CONNECTOR]")
            connector = get_object_or_404(Connector, code=connector_code)
            line_id = data.get("data[LINE]")
            message_id = data.get("data[MESSAGES][0][im][message_id]")
            chat_id = data.get("data[MESSAGES][0][im][chat_id]")
            chat = data.get("data[MESSAGES][0][chat][id]")
            send_result = None

            
            file_type = data.get("data[MESSAGES][0][message][files][0][type]", None)
            text = data.get("data[MESSAGES][0][message][text]", "")
            command_text = ""
            
            quoted_msg_id = None
            if text:
                # Поиск ID цитируемого сообщения (wamid.ID.ext)
                match = re.search(r"wamid\.([a-zA-Z0-9_]+)\.", text)
                if match:
                    short_id = match.group(1)
                    # Lookup full message ID from Redis
                    full_id = redis_client.get(f"wamid:{short_id}")
                    if full_id:
                        quoted_msg_id = full_id.decode('utf-8')
                    else:
                        # Fallback: try using the ID directly if not found in Redis (legacy behavior)
                        quoted_msg_id = short_id

                excludes_raw = appinstance.exclude or ''
                excludes = [e.strip() for e in excludes_raw.split(",") if e.strip()]
                if any(ex.lower() in text.lower() for ex in excludes):
                    return "message filtered"
                text = text.replace("[br]", "\n")
                text = re.sub(r"\[/?[a-zA-Z*][a-zA-Z0-9*]*\]|\[[a-zA-Z0-9\s]+=[^\]]+\]", "", text)
                command_lines = [line.strip() for line in text.splitlines() if line.strip()]
                if command_lines:
                    command_text = command_lines[-1].lower()

            files = []
            if file_type:
                files = extract_files(data)
            
            # If WABA connector
            if connector.service == "waba":
                phone = (
                    Phone.objects.select_related("line", "line__connector", "line__portal", "waba")
                    .filter(
                        line__line_id=line_id,
                        line__portal=appinstance.portal,
                        line__connector__service="waba",
                    )
                    .first()
                )
                if not phone:
                    error_result = {"error": True, "message": "WABA phone not found for Bitrix line"}
                    send_waba_error_to_openline(appinstance.id, chat, error_result, connector.code, line_id)
                    return error_result["message"]

                if not files and command_text in ["#wa_block", "#wa_unblock"]:
                    try:
                        send_result = parse_block_command(command_text, phone, chat, appinstance.id, chat_id)
                    except Exception as e:
                        return {"error": True, "message": str(e)}
                    if "error" not in (send_result or {}):
                        send_delivery_status(appinstance.id, connector_code, line_id, chat_id, message_id)
                    return send_result

                message = {
                    "messaging_product": "whatsapp",
                    "biz_opaque_callback_data": {"bitrix_user_id": user_id},
                    "to": chat,
                }
                
                if quoted_msg_id:
                    message["context"] = {"message_id": quoted_msg_id}

                # Обработка шаблонных сообщений
                if "template+" in text:
                    template_start = text.index("template+")
                    template_str = text[template_start:]
                    message.update(parse_template_code(template_str, appinstance=appinstance, line_id=line_id))

                elif "interactive+" in text:
                    interactive_start = text.index("interactive+")
                    interactive_str = text[interactive_start:]
                    try:
                        message.update(parse_interactive_code(interactive_str, appinstance=appinstance, phone=phone))
                    except Exception as e:
                        error_result = {"error": True, "message": str(e)}
                        send_waba_error_to_openline(appinstance.id, chat, error_result, connector.code, line_id)
                        raise

                elif hashtag_interactive := _get_hashtag_interactive(text, appinstance):
                    message.update(parse_interactive_code(f"interactive+{hashtag_interactive.id}", appinstance=appinstance, phone=phone))
                elif not files and text:
                    message["type"] = "text"
                    message["text"] = {"body": text}

                # Если есть файлы, отправляем сообщение с каждым файлом отдельно
                if files:
                    media_caption = text.strip() if text else ""
                    for file in files:
                        uploaded_id = None
                        waba_file_type = _get_waba_file_type(file)
                        try:
                            if waba_file_type == "link":
                                logger.info("B24->WABA file as link: %s %s", file.get("name"), file.get("link"))
                            elif appinstance.fileAsUrl:
                                logger.info("B24->WABA file as URL: %s %s", file.get("name"), file.get("link"))
                            else:
                                # Reuse media uploaded from the same file URL before downloading it.
                                uploaded_id = waba.get_cached_media_id_for_phone(phone, file["link"])
                                if uploaded_id:
                                    logger.info("B24->WABA file cached media id: %s %s", file.get("name"), uploaded_id)
                            if waba_file_type != "link" and not appinstance.fileAsUrl and not uploaded_id:
                                up_res = _upload_waba_file_with_retries(phone, file)
                                if up_res and "id" in up_res:
                                    uploaded_id = up_res["id"]
                                    logger.info("B24->WABA file streaming uploaded: %s %s", file.get("name"), uploaded_id)
                                else:
                                    logger.info("B24->WABA file streaming upload failed: %s %s", file.get("name"), up_res)
                        except Exception as e:
                            logger.error(f"Upload failed: {e}")

                        # Определяем тип файла и добавляем его к сообщению
                        if waba_file_type == "link":
                            message["type"] = "text"
                            link = file.get("source_link") or file.get("link") or file.get("download_link") or ""
                            body_parts = [
                                part for part in [
                                    file.get("name"),
                                    file.get("sizef"),
                                    link,
                                ] if part
                            ]
                            body = "\n".join(body_parts)
                            if media_caption:
                                body = f"{media_caption}\n{body}" if body else media_caption
                            message["text"] = {"body": body}
                        elif waba_file_type == "image":
                            message["type"] = "image"
                            if uploaded_id:
                                message["image"] = {"id": uploaded_id}
                            else:
                                if not appinstance.fileAsUrl:
                                    logger.info("B24->WABA file fallback as URL: %s %s", file.get("name"), file.get("link"))
                                message["image"] = {"link": file["link"]}
                            if media_caption:
                                message["image"]["caption"] = media_caption
                        elif waba_file_type == "video":
                            message["type"] = "video"
                            if uploaded_id:
                                message["video"] = {"id": uploaded_id}
                            else:
                                if not appinstance.fileAsUrl:
                                    logger.info("B24->WABA file fallback as URL: %s %s", file.get("name"), file.get("link"))
                                message["video"] = {"link": file["link"]}
                            if media_caption:
                                message["video"]["caption"] = media_caption
                        elif waba_file_type == "audio":
                            message["type"] = "audio"
                            if uploaded_id:
                                message["audio"] = {"id": uploaded_id}
                            else:
                                if not appinstance.fileAsUrl:
                                    logger.info("B24->WABA file fallback as URL: %s %s", file.get("name"), file.get("link"))
                                message["audio"] = {"link": file["link"]}
                        elif waba_file_type == "document":
                            message["type"] = "document"
                            if uploaded_id:
                                message["document"] = {"id": uploaded_id, "filename": file["name"]}
                            else:
                                if not appinstance.fileAsUrl:
                                    logger.info("B24->WABA file fallback as URL: %s %s", file.get("name"), file.get("link"))
                                message["document"] = {"link": file["link"], "filename": file["name"]}
                            if media_caption:
                                message["document"]["caption"] = media_caption

                        send_result = waba.send_message_from_phone(phone, message)
                        if "error" in (send_result or {}):
                            if self.request.retries >= self.max_retries:
                                send_waba_error_to_openline(appinstance.id, chat, send_result, connector.code, line_id)
                            raise_waba_send_error(send_result)

                else:
                    send_result = waba.send_message_from_phone(phone, message)
                    if "error" in (send_result or {}):
                        if self.request.retries >= self.max_retries:
                            send_waba_error_to_openline(appinstance.id, chat, send_result, connector.code, line_id)
                        raise_waba_send_error(send_result)

            elif connector.service == "waweb":
                try:
                    line = Line.objects.get(line_id=line_id, portal=appinstance.portal)
                    wa = Session.objects.get(line=line)
                    if files:
                        for file in files:
                            waweb_tasks.send_message(str(wa.session), chat, file, 'media')
                    else:
                        send_result = waweb_tasks.send_message(wa.session, chat, text)
                except Exception as e:
                    raise

            # If OLX connector
            elif connector.service == "olx":
                try:
                    send_result = olx_tasks.send_message(chat, text, files)
                except Exception as e:
                    send_error_to_openline(appinstance.id, chat, str(e), connector.code, line_id)
                    raise

            send_delivery_status(appinstance.id, connector_code, line_id, chat_id, message_id)

            return send_result
        
        elif event == "ONIMCONNECTORSTATUSDELETE":
            line_id = data.get("data[line]")
            connector_code = data.get("data[connector]")
            connector = get_object_or_404(Connector, code=connector_code)
            line = get_object_or_404(Line, line_id=line_id, portal=appinstance.portal)

            if connector.service == "olx":
                olxuser = line.olx_users.first()
                if olxuser:
                    olxuser.line = None
                    olxuser.save()

            elif connector.service == "waba":
                phone = line.phones.first()
                if phone:
                    phone.line = None
                    phone.save()
            
            elif connector.service == "waweb":
                phone = line.wawebs.first()
                if phone:
                    phone.line = None
                    phone.save()

        elif event == "ONIMCONNECTORLINEDELETE":
            line_id = data.get("data")
            line = get_object_or_404(Line, line_id=line_id, portal=appinstance.portal)
            line.delete()
            
        # AsterX
        elif event == "ONEXTERNALCALLSTART":
            try:
                pbx = Server.objects.filter(settings__app_instance=appinstance).first()
                b24_user_id = data.get('data[USER_ID]')
                phone_number = data.get('data[PHONE_NUMBER_INTERNATIONAL]')
                call_id = data.get('data[CALL_ID]')
                payload = {
                    'event': event,
                    'b24_user_id': b24_user_id,
                    'phone_number': phone_number,
                    'call_id': call_id,
                }
                send_call_info(pbx.id, payload)
            except Exception as e:
                raise
        
        elif event == "ONEXTERNALCALLBACKSTART":
            try:
                pbx = Server.objects.filter(settings__app_instance=appinstance).first()
                phone_number = data.get('data[PHONE_NUMBER]')
                payload = {
                    'event': event,
                    'phone_number': phone_number,
                }
                send_call_info(pbx.id, payload)
            except Exception as e:
                raise

        # BitBot
        elif event in ["ONIMBOTMESSAGEADD", "ONIMCOMMANDADD", "ONIMBOTJOINCHAT"]:
            bitbot_router.event_processor.delay(data)

        elif event == "ONAPPUNINSTALL":
            appinstance.delete()

    except TRANSIENT_ERRORS:
        raise
    except Exception:
        raise


def save_temp_file(file_content, filename, app_instance):
    """
    Saves file content locally and returns a signed URL for Bitrix to download.
    file_content: bytes
    host: optional, override domain for file url
    """
    try:
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)

        file_path = os.path.join(temp_dir, filename)

        with open(file_path, 'wb') as f:
            f.write(file_content)

        signer = TimestampSigner()
        signed_path = signer.sign(filename)
        base_url = getattr(settings, "BITRIX_TEMP_FILE_BASE_URL", "").strip().rstrip("/")
        if base_url:
            file_url = f"{base_url}{settings.MEDIA_URL}temp/?{signed_path}"
        else:
            # Accept host override via app_instance.host if present, else use app_instance.app.site.domain
            domain = getattr(app_instance, 'host', None)
            if not domain:
                domain = app_instance.app.site.domain
            # Ensure domain doesn't have protocol
            domain = domain.replace("http://", "").replace("https://", "").strip("/")
            file_url = f"https://{domain}{settings.MEDIA_URL}temp/?{signed_path}"

        # Schedule deletion after configured TTL
        ttl = getattr(settings, 'BITRIX_TEMP_FILE_TTL', 1800)
        try:
            bitrix_tasks.delete_temp_file.apply_async(args=[file_path], countdown=ttl)
        except Exception:
            pass

        return file_url

    except Exception as e:
        logger.error(f"Error handling temp file: {e}")
        return None

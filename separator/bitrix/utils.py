import base64
import time
import json
import logging
import re
import uuid
import os
import redis
import requests
from django.core.signing import TimestampSigner
from urllib.parse import unquote
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.contrib import messages
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.http import HttpResponse

from celery import shared_task

import separator.olx.tasks as olx_tasks
import separator.waba.utils as waba

from separator.waweb.models import Session
import separator.waweb.tasks as waweb_tasks

from .models import App, AppInstance, Bitrix, Line, VerificationCode, Connector, Credential
from .models import User as B24_user

import separator.bitrix.tasks as bitrix_tasks

import separator.bitbot.router as bitbot_router

if settings.ASTERX_SERVER:
    from separator.asterx.models import Server
    from separator.asterx.utils import send_call_info

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


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
        return App.objects.get(client_id=client_id)
    except Exception as e:
        raise


def get_instances(request, service=None):
    b24_users = B24_user.objects.filter(owner=request.user, admin=True).all()
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


def get_b24_user(app: App, portal: Bitrix, auth_id, refresh_id):
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
                "refresh_token": refresh_id,
            }
        )
        if not created:
            cred.access_token = auth_id
            cred.refresh_token = refresh_id
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
        if entity.line:
            bitrix_tasks.call_api(app_instance.id, "imconnector.activate", {
                "CONNECTOR": connector.code,
                "LINE": entity.line.line_id,
                "ACTIVE": 0,
            })
        if connector.service == "olx":
            line_name = entity.olx_id
        else:
            line_name = entity.phone
        create_payload = {
            "PARAMS": {
                "LINE_NAME": line_name,
                "ACTIVE": "Y",
                "WELCOME_MESSAGE": "N",
                "CLOSE_RULE": "none",
                "VOTE_MESSAGE": "N"
            }
        }
        result = bitrix_tasks.call_api(app_instance.id, "imopenlines.config.add", create_payload)
        if result and result.get("result"):
            new_line_id = result["result"]
            line = Line.objects.create(
                line_id=new_line_id,
                portal=app_instance.portal,
                connector=connector,
                app_instance=app_instance,
                owner=app_instance.owner
            )
            entity.line = line
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
        app_instance = line.app_instance
        connector = app_instance.app.connectors.filter(service=connector_service).first()
        bitrix_tasks.messageservice_add.delay(app_instance.id, entity.id, connector.service)       
        if entity.line:
            if str(entity.line.id) == str(line_id):
                messages.warning(request, "Эта линия уже используется.")
                return
            bitrix_tasks.call_api(app_instance.id, "imconnector.activate", {
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
            entity.line = line
            entity.app_instance = app_instance
            entity.save()
            messages.success(request, "Линия подключена")


# Подписка на события
def events_bind(appinstance: AppInstance):
    url = appinstance.app.site
    for event in appinstance.app.events.strip().splitlines():
        payload = {
            "event": event,
            "HANDLER": f"https://{url}/api/bitrix/",
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
            "PLACEMENT_HANDLER": f"https://{url}/app-settings/?inst={appinstance.id}",
        }

        bitrix_tasks.call_api.delay(appinstance.id, "imconnector.register", payload)

    except FileNotFoundError:
        return None
    except Exception as e:
        return None


def extract_files(data):
    files = []
    i = 0
    while True:
        # Формируем ключи для доступа к данным файлов
        name_key = f"data[MESSAGES][0][message][files][{i}][name]"
        link_key = f"data[MESSAGES][0][message][files][{i}][link]"
        type_key = f"data[MESSAGES][0][message][files][{i}][type]"

        # Проверяем, существуют ли такие ключи в словаре
        if name_key in data and link_key in data:
            # Добавляем название и ссылку в список
            files.append(
                {
                    "name": data.get(name_key),
                    "link": data.get(link_key),
                    "type": data.get(type_key),
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


def process_placement(request):
    try:
        data = request.POST
        placement_options = data.get("PLACEMENT_OPTIONS")
        instance_id = request.GET.get("inst")
        domain = request.GET.get("DOMAIN")

        placement_options = json.loads(placement_options)
        line_id = placement_options.get("LINE")
        connector_code = placement_options.get("CONNECTOR")

        app_instance = AppInstance.objects.filter(id=instance_id).first()
        if not app_instance:
            return HttpResponse("app not found")
        portal = Bitrix.objects.filter(domain=domain).first()
        if not portal:
            return HttpResponse("bitrix not found")
        connector = Connector.objects.filter(code=connector_code).first()
        if not connector:
            return HttpResponse("connector not found")
        line, created = Line.objects.get_or_create(
            line_id=line_id,
            portal=portal,
            connector=connector,
            app_instance=app_instance,
            owner=app_instance.owner
        )
        return HttpResponse(
            f"Линия изменена, настройте линию https://{app_instance.app.site}/portals/"
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return HttpResponse({"An unexpected error occurred"})

CALL_REQUEST = {
    "type": "interactive",
    "recipient_type": "individual",
    "interactive": {
        "type": "call_permission_request",
        "action": {
            "name": "call_permission_request"
        }
    }    
}


def parse_template_code(code: str, appinstance=None, line_id=None, phone_num=None) -> dict:
    try:
        parts = code.split("+")
        if len(parts) < 3:
            raise ValueError("Invalid message body format")
        _, template_name, language, *other = parts
        params = []
        file_url = None
        file_type = None
        waba_file_type = None
        button_param = None

        for p in other:
            p = p.strip()
            if p.startswith('file_link:'):
                file_url = p[len('file_link:'):]
                file_headers = None
                try:
                    file_headers = requests.head(file_url, allow_redirects=True, timeout=10)
                    file_type = file_headers.headers.get('Content-Type', '')
                except Exception:
                    file_type = ''
                if file_type.startswith('image/'):
                    waba_file_type = "image"
                elif file_type.startswith('video/'):
                    waba_file_type = "video"
                elif file_type == "application/pdf" or file_url.lower().endswith('.pdf'):
                    waba_file_type = "document"
                else:
                    waba_file_type = "document"
            elif p.startswith('button_param:'):
                button_param = p[len('button_param:'):]
            elif p:
                if "|" in p:
                    params.extend([x.strip() for x in p.split("|") if x.strip()])
                else:
                    params.append(p)

        message = {
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }
        components = []
        if file_url and waba_file_type:
            uploaded_id = None
            filename = "file"
            if waba_file_type == "document":
                filename = "file.pdf"
                if file_headers:
                    cd = file_headers.headers.get('Content-Disposition', '')
                    m = re.search(r"filename\*=utf-8''(.+)", cd)
                    if m:
                        filename = unquote(m.group(1))
                    else:
                        m = re.search(r'filename="(.+?)"', cd)
                        if m:
                            filename = m.group(1)
            else:
                ext = file_type.split('/')[-1] if '/' in file_type else 'bin'
                filename = f"file.{ext}"

            if appinstance and (line_id or phone_num):
                try:
                    r = requests.get(file_url, timeout=30)
                    if r.status_code == 200:
                        up_res = waba.upload_media(appinstance, r.content, file_type, filename, line_id=line_id, phone_num=phone_num)
                        if up_res and "id" in up_res:
                            uploaded_id = up_res["id"]
                except Exception as e:
                    logger.error(f"Template media upload failed: {e}")

            file_param = {
                "type": waba_file_type
            }
            if uploaded_id:
                file_param[waba_file_type] = {"id": uploaded_id}
            else:
                file_param[waba_file_type] = {"link": file_url}

            if waba_file_type == "document":
                file_param[waba_file_type]["filename"] = filename
            
            components.append({
                "type": "header",
                "parameters": [file_param]
            })
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


@shared_task(queue='bitrix')
def sms_processor(data, service):
    application_token = data.get("auth[application_token]")
    app_instance = AppInstance.objects.filter(application_token=application_token).first()
    if not app_instance:
        raise Exception("app not found")
    message_body = data.get("message_body")
    user_id = data.get("auth[user_id]")
    code = data.get("code", {})
    sender = code.split('_')[-1]
    message_to = re.sub(r'\D', '', data.get("message_to"))
    message_id = data.get("message_id")
    line = None
    status = None
    send_result = None
    try:
        if service == "waba":
            message = {
                "messaging_product": "whatsapp",
                "biz_opaque_callback_data": {
                    "bitrix_user_id": user_id,
                    "sms_message_id": message_id
                }
            }
            if message_body.startswith("template+"):
                message.update(parse_template_code(message_body, appinstance=app_instance, phone_num=sender))
            elif message_body == "#call_permission_request":
                message.update(CALL_REQUEST)
            else:
                message.update(
                    {
                        "type": "text",
                        "text": {
                            "body": message_body
                        }
                    }
                )
            if message:
                message['to'] = message_to
                send_result = waba.send_message(app_instance, message, phone_num=sender)
                if not "error" in send_result:
                    pass
        elif service == "waweb":
            try:
                wa = Session.objects.get(phone=sender)
                line = wa.line
                send_result = waweb_tasks.send_message(wa.session, message_to, message_body)
                status = "delivered"
            except wa.DoesNotExist:
                send_result = {"error": True, "message": f"No Session found for phone number: {code}"}
            except Exception as e:
                send_result = {"error": True, "message": {e}}
    except Exception as e:
        send_result = {"error": True, "message": str(e)}
    finally:
        is_waba_success = service == "waba" and send_result and "error" not in send_result
        
        if not is_waba_success:
            status_data = {
                "CODE": code,
                "MESSAGE_ID": message_id,
                "STATUS": status if status else "failed",
            }
            bitrix_tasks.call_api.delay(app_instance.id, "messageservice.message.status.update", status_data)

        if line and status:
            bitrix_tasks.message_add.delay(app_instance.id, line.line_id, message_to, message_body, line.connector.code)
    if isinstance(send_result, dict) and send_result.get("error"):
        error_msg = send_result.get("message")
        if service == "waba":
            try:
                import ast
                error_data = ast.literal_eval(error_msg)
                if isinstance(error_data, dict):
                    if "error" in error_data:
                        adapted_data = {"errors": [error_data["error"]], "recipient_id": message_to}
                        error_msg = waba.error_message(adapted_data)
                    elif "errors" in error_data:
                        error_data["recipient_id"] = message_to
                        error_msg = waba.error_message(error_data)
            except Exception:
                pass

        payload = {
            "USER_ID": user_id,
            "MESSAGE": error_msg
        }
        bitrix_tasks.call_api.delay(app_instance.id, "im.notify.system.add", payload)
        raise ValueError(send_result)
    if hasattr(send_result, "json"):
        return send_result.json()
    return send_result

@shared_task(queue='bitrix')
def event_processor(data):
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
        # Проверка наличия установки
        try:
            appinstance = AppInstance.objects.get(application_token=application_token)

        except AppInstance.DoesNotExist:
            if event == "ONAPPINSTALL":                
                # Получение приложения по токену
                try:
                    app = get_app(access_token)
                except Exception:
                    raise
                portal, created = Bitrix.objects.get_or_create(
                    member_id=member_id,
                    defaults={
                        "domain": domain,
                    }
                )

                appinstance_data = {
                    "app": app,
                    "portal": portal,
                    "auth_status": auth_status,
                    "application_token": application_token,
                    "owner": portal.owner,
                }

                appinstance = AppInstance.objects.create(**appinstance_data)

                try:
                    b24_user = get_b24_user(app, portal, access_token, refresh_token)
                    if portal.owner and not b24_user.owner:
                        b24_user.owner = portal.owner
                        b24_user.save()
                except Exception as e:
                    pass

                # Получаем storage_id и сохраняем его
                if "disk" in scope:
                    storage_data = bitrix_tasks.call_api(appinstance.id, "disk.storage.getforapp", {})
                    if "result" in storage_data:
                        storage_id = storage_data["result"]["ID"]
                        appinstance.storage_id = storage_id
                        appinstance.save()

                # Регистрация коннектора/ подписка на события
                def register_events_and_connectors():
                    events_bind(appinstance)
                    if app.connectors.exists():
                        for connector in app.connectors.all():
                            register_connector(appinstance, connector)

                transaction.on_commit(register_events_and_connectors)

                if settings.ASTERX_SERVER and app.asterx:
                    from separator.asterx.views import get_portal_settings
                    get_portal_settings(member_id)

                # Если портал уже прявязан
                if portal.owner:
                    return "App successfully created and linked"
                
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
                raise

        if event == "ONIMCONNECTORMESSAGEADD":
            connector_code = data.get("data[CONNECTOR]")
            connector = get_object_or_404(Connector, code=connector_code)
            line_id = data.get("data[LINE]")
            message_id = data.get("data[MESSAGES][0][im][message_id]")
            chat_id = data.get("data[MESSAGES][0][im][chat_id]")
            chat = data.get("data[MESSAGES][0][chat][id]")

            # Проверяем наличие сообщения в редис (отправлено из других сервисов )
            for _ in range(5):
                if redis_client.exists(f'bitrix:{member_id}:{message_id}'):
                    raise Exception('loop message')
                time.sleep(1)
            
            file_type = data.get("data[MESSAGES][0][message][files][0][type]", None)
            text = data.get("data[MESSAGES][0][message][text]", "")
            
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
                    raise Exception("message filtered")
                text = text.replace("[br]", "\n")
                text = re.sub(r"\[/?[a-zA-Z*][a-zA-Z0-9*]*\]|\[[a-zA-Z0-9\s]+=[^\]]+\]", "", text)

            files = []
            if file_type:
                files = extract_files(data)
            if appinstance.fileAsUrl:
                msg = '\n'.join([f"{f['name']}: {f['link']}" for f in files])
                text = f"{text} {msg}"
                files = []
            
            # If WABA connector
            if connector.service == "waba":
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

                elif "#call_permission_request" in text:
                    message.update(CALL_REQUEST)
                elif not files and text:
                    message["type"] = "text"
                    message["text"] = {"body": text}

                # Если есть файлы, отправляем сообщение с каждым файлом отдельно
                if files:
                    for file in files:
                        uploaded_id = None
                        try:
                            f_content = None
                            # Simple retry logic for file download
                            for attempt in range(3):
                                try:
                                    f_content = requests.get(file["link"], timeout=(10, 60))
                                    if f_content.status_code == 200:
                                        break
                                except requests.RequestException:
                                    if attempt == 2:
                                        raise
                                    time.sleep(1)
                            
                            if f_content and f_content.status_code == 200:
                                up_res = waba.upload_media(
                                    appinstance, 
                                    f_content.content, 
                                    f_content.headers.get("Content-Type", ""), 
                                    file["name"], 
                                    line_id=line_id
                                )
                                if up_res and "id" in up_res:
                                    uploaded_id = up_res["id"]
                        except Exception as e:
                            logger.error(f"Upload failed: {e}")

                        # Определяем тип файла и добавляем его к сообщению
                        if file["type"] == "image":
                            message["type"] = "image"
                            if uploaded_id:
                                message["image"] = {"id": uploaded_id}
                            else:
                                message["image"] = {"link": file["link"]}
                        elif file["type"] in ["file", "video", "audio"]:
                            message["type"] = "document"
                            if uploaded_id:
                                message["document"] = {
                                    "id": uploaded_id,
                                    "filename": file["name"],
                                }
                            else:
                                message["document"] = {
                                    "link": file["link"],
                                    "filename": file["name"],
                                }

                        send_result = waba.send_message(appinstance, message, line_id=line_id)
                        if "error" in send_result:
                            # Send error message back to chat
                            error_msg = send_result.get("message", "Unknown error")
                            try:
                                import ast
                                error_data = ast.literal_eval(str(error_msg))
                                if isinstance(error_data, dict):
                                    if "error" in error_data:
                                        adapted_data = {"errors": [error_data["error"]], "recipient_id": "Error"}
                                        error_msg = waba.error_message(adapted_data)
                            except Exception:
                                pass
                            
                            bitrix_tasks.message_add.delay(
                                appinstance.id, 
                                line_id, 
                                chat, 
                                f"[color=#ff0000]{error_msg}[/color]", 
                                connector.code
                            )
                            raise Exception(send_result)

                else:
                    send_result = waba.send_message(appinstance, message, line_id=line_id)
                    if "error" in send_result:
                         # Send error message back to chat
                        error_msg = send_result.get("message", "Unknown error")
                        try:
                            import ast
                            error_data = ast.literal_eval(str(error_msg))
                            if isinstance(error_data, dict):
                                if "error" in error_data:
                                    adapted_data = {"errors": [error_data["error"]], "recipient_id": "Error"}
                                    error_msg = waba.error_message(adapted_data)
                        except Exception:
                            pass

                        bitrix_tasks.message_add.delay(
                            appinstance.id, 
                            line_id, 
                            chat, 
                            f"[color=#ff0000]{error_msg}[/color]", 
                            connector.code
                        )
                        raise Exception(send_result)

            elif connector.service == "waweb":
                try:
                    line = Line.objects.get(line_id=line_id, app_instance=appinstance)
                    wa = Session.objects.get(line=line)
                    if files:
                        for file in files:
                            waweb_tasks.send_message(str(wa.session), chat, file, 'media')
                    else:
                        waweb_tasks.send_message(wa.session, chat, text)
                except Exception as e:
                    raise

            # If OLX connector
            elif connector.service == "olx":
                try:
                    olx_tasks.send_message(chat, text, files)
                except Exception:
                    raise

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

            bitrix_tasks.call_api.delay(appinstance.id, "imconnector.send.status.delivery", status_data)
        
        elif event == "ONIMCONNECTORSTATUSDELETE":
            line_id = data.get("data[line]")
            connector_code = data.get("data[connector]")
            connector = get_object_or_404(Connector, code=connector_code)
            line = get_object_or_404(Line, line_id=line_id, app_instance=appinstance)

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
            line = get_object_or_404(Line, line_id=line_id, app_instance=appinstance)
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
        elif event in ["ONIMBOTMESSAGEADD", "ONIMCOMMANDADD"]:
            # from pprint import pprint
            # from datetime import datetime
            # filename = f'logs/{str(datetime.now().timestamp())}.json'
            # with open(filename, 'w', encoding='utf-8') as f:
            #     pprint(data, stream=f)
            # pass
            bitbot_router.event_processor.delay(data)

        elif event == "ONAPPUNINSTALL":
            portal = appinstance.portal
            appinstance.delete()
            if not AppInstance.objects.filter(portal=portal).exists():
                portal.delete()

    except Exception as e:
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

        # Accept host override via app_instance.host if present, else use app_instance.app.site.domain
        domain = getattr(app_instance, 'host', None)
        if not domain:
            domain = app_instance.app.site.domain
        # Ensure domain doesn't have protocol
        domain = domain.replace("http://", "").replace("https://", "").strip("/")

        signer = TimestampSigner()
        signed_path = signer.sign(filename)

        file_url = f"https://{domain}{settings.MEDIA_URL}temp/?{signed_path}"

        # Schedule deletion after configured TTL
        ttl = getattr(settings, 'BITRIX_TEMP_FILE_TTL', 1800)
        bitrix_tasks.delete_temp_file.apply_async(args=[file_path], countdown=ttl)

        return file_url

    except Exception as e:
        logger.error(f"Error handling temp file: {e}")
        return None

def upload_and_get_link(appinstance, file_content_bytes, filename):
    """
    Uploads file to Bitrix Disk and returns a permanent external link.
    Used for system messages (echoes) that need to persist in chat history.
    """
    try:
        file_b64 = base64.b64encode(file_content_bytes).decode("utf-8")
        upload_res = upload_file(appinstance, appinstance.storage_id, file_b64, filename)
        
        if upload_res:
            file_id = upload_res.get("ID")
            if file_id:
                link_res = bitrix_tasks.call_api(appinstance.id, "disk.file.getExternalLink", {"id": file_id})
                if link_res and "result" in link_res:
                    return link_res.get("result")
    except Exception as e:
        logger.error(f"Error uploading file to Bitrix Disk: {e}")
    return None

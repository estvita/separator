import base64
import time
import json
import logging
import re
import uuid
import redis
import requests
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.contrib import messages
from django.conf import settings
from django.shortcuts import get_object_or_404

from celery import shared_task

from rest_framework import status
from rest_framework.response import Response
from django.http import HttpResponse
from rest_framework.authtoken.models import Token

import thoth.olx.tasks as olx_tasks
import thoth.waba.utils as waba

from thoth.waweb.models import Session
import thoth.waweb.tasks as waweb_tasks

from .models import App, AppInstance, Bitrix, Line, VerificationCode, Connector, Credential
from .models import User as B24_user

import thoth.bitrix.tasks as bitrix_tasks

if settings.ASTERX_SERVER:
    from thoth.asterx.models import Server
    from thoth.asterx.utils import send_call_info

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

VENDOR_BITRIX_INSTANCE = settings.VENDOR_BITRIX_INSTANCE

logger = logging.getLogger("django")

def get_instances(request, connector_service):
    """
    Возвращает queryset AppInstance по порталу из сессии или пользователю и сервису коннектора.
    """
    b24_data = request.session.get('b24_data')
    portal = None
    if b24_data:
        member_id = b24_data.get("member_id")
        if member_id:
            b24_user = B24_user.objects.filter(owner=request.user, bitrix__member_id=member_id).first()
            portal = b24_user.bitrix
    if portal:
        return AppInstance.objects.filter(portal=portal, app__connectors__service=connector_service).distinct()
    return AppInstance.objects.filter(owner=request.user, app__connectors__service=connector_service).distinct()


def get_b24_user(app: App, portal: Bitrix, auth_id, refresh_id):
    try:
        profile = requests.post(f"{portal.protocol}://{portal.domain}/rest/profile", json={"auth": auth_id})
        profile_data = profile.json().get("result")
        admin = profile_data.get("ADMIN")
        user_id = profile_data.get("ID")
    except Exception as e:
        raise
    
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
        if entity.line and str(entity.line.id) == str(line_id):
            messages.warning(request, "Эта линия уже используется.")
            return
        if entity.line:
            bitrix_tasks.call_api.id(app_instance.id, "imconnector.activate", {
                "CONNECTOR": connector.code,
                "LINE": entity.line.line_id,
                "ACTIVE": 0,
            })
        response = bitrix_tasks.call_api.id(app_instance.id, "imconnector.activate", {
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
    api_key, created = Token.objects.get_or_create(user=appinstance.app.owner)
    for event in appinstance.app.events.strip().splitlines():
        payload = {
            "event": event,
            "HANDLER": f"https://{url}/api/bitrix/?api-key={api_key}",
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

@shared_task(queue='bitrix')
def sms_processor(data, service):
    application_token = data.get("auth[application_token]")
    app_instance = AppInstance.objects.filter(application_token=application_token).first()
    if not app_instance:
        raise Exception("app not found")
    message_body = data.get("message_body")
    code = data.get("code", {})
    sender = code.split('_')[-1]
    message_to = re.sub(r'\D', '', data.get("message_to"))

    status_data = {
        "CODE": code,
        "MESSAGE_ID": data.get("message_id"),
        "STATUS": "delivered"
    }

    bitrix_tasks.call_api(app_instance.id, "messageservice.message.status.update", status_data)

    line = None

    if service == "waba":
        message = None
        if message_body.startswith("template+"):
            try:
                _, template_name, language = message_body.split("+", 2)
            except ValueError as e:
                raise ValueError("Invalid message body format")
            message = {
                "type": "template",
                "template": {"name": template_name, "language": {"code": language}},
            }
        elif message_body == "#call_permission_request":
            message = CALL_REQUEST
        if message:
            message['to'] = message_to
            waba.send_message(app_instance, message, phone_num=sender)
    
    elif service == "waweb":
        try:
            wa = Session.objects.get(phone=sender)
            line = wa.line
            waweb_tasks.send_message(wa.session, message_to, message_body)
        except wa.DoesNotExist:
            raise ValueError(f"No Session found for phone number: {code}")
        except Exception as e:
            raise ValueError(e)

    if line:
        bitrix_tasks.message_add.delay(app_instance.id, line.line_id, message_to, message_body, line.connector.code)
    return Response({"status": "message processed"}, status=status.HTTP_200_OK)


@shared_task(queue='bitrix')
def event_processor(data, app_id=None, user_id=None):
    try:
        event = data.get("event").upper()
        domain = data.get("auth[domain]")
        user_id = data.get("auth[user_id]")
        auth_status = data.get("auth[status]")
        access_token = data.get("auth[access_token]")
        refresh_token = data.get("auth[refresh_token]")
        application_token = data.get("auth[application_token]")
        member_id = data.get("auth[member_id]")

        # Проверка наличия установки
        try:
            appinstance = AppInstance.objects.get(application_token=application_token)

        except AppInstance.DoesNotExist:
            if event == "ONAPPINSTALL":                
                
                # Получение приложения по app_id
                app = get_object_or_404(App, id=app_id)                
                owner_user = request.user if auth_status == "L" else None
                portal, created = Bitrix.objects.get_or_create(
                    member_id=member_id,
                    defaults={
                        "domain": domain,
                        "owner": owner_user,
                    }
                )

                appinstance_data = {
                    "app": app,
                    "portal": portal,
                    "auth_status": auth_status,
                    "application_token": application_token,
                    "owner": owner_user,
                }

                appinstance = AppInstance.objects.create(**appinstance_data)

                try:
                    b24_user = get_b24_user(app, portal, access_token, refresh_token)
                    if owner_user and not b24_user.owner:
                        b24_user.owner = owner_user
                        b24_user.save()
                except Exception as e:
                    pass

                # Получаем storage_id и сохраняем его
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
                    from thoth.asterx.views import get_portal_settings
                    get_portal_settings(member_id)

                if VENDOR_BITRIX_INSTANCE:
                    bitrix_tasks.create_deal.delay(appinstance.id, VENDOR_BITRIX_INSTANCE, app.name)

                # Если портал уже прявязан
                if portal.owner:
                    return "App successfully created and linked"
                
                verify_code = VerificationCode.objects.filter(
                    portal=portal,
                ).first()

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

        # Обработка события ONIMCONNECTORMESSAGEADD
        if event == "ONIMCONNECTORMESSAGEADD":
            connector_code = data.get("data[CONNECTOR]")
            connector = get_object_or_404(Connector, code=connector_code)
            line_id = data.get("data[LINE]")
            message_id = data.get("data[MESSAGES][0][im][message_id]")
            chat_id = data.get("data[MESSAGES][0][im][chat_id]")
            chat = data.get("data[MESSAGES][0][chat][id]")
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

            bitrix_tasks.call_api(appinstance.id, "imconnector.send.status.delivery", status_data)

            # Проверяем наличие сообщения в редис (отправлено из других сервисов )
            for _ in range(5):
                if redis_client.exists(f'bitrix:{member_id}:{message_id}'):
                    raise Exception('loop message')
                time.sleep(1)
            
            file_type = data.get("data[MESSAGES][0][message][files][0][type]", None)
            text = data.get("data[MESSAGES][0][message][text]", None)
            if text:
                excludes_raw = appinstance.exclude or ''
                excludes = [e.strip() for e in excludes_raw.split(",") if e.strip()]
                if any(ex.lower() in text.lower() for ex in excludes):
                    raise Exception("message filtered")
                text = re.sub(r"\[(?!(br|\n))[^\]]+\]", "", text)
                text = text.replace("[br]", "\n")

            files = []
            if file_type:
                files = extract_files(data)

            # If WABA connector
            if connector.service == "waba":
                message = {
                    "biz_opaque_callback_data": f"{line_id}_{chat_id}_{message_id}",
                    "to": chat,
                }
                # Обработка шаблонных сообщений
                if "template-" in text:
                    _, template_body = text.split("-")
                    template, language = template_body.split("+")
                    message["type"] = "template"
                    message["template"] = {
                        "name": template,
                        "language": {"code": language},
                    }
                elif "#call_permission_request" in text:
                    message.update(CALL_REQUEST)
                elif not files and text:
                    message["type"] = "text"
                    message["text"] = {"body": text}

                # Если есть файлы, отправляем сообщение с каждым файлом отдельно
                if files:
                    for file in files:
                        # Определяем тип файла и добавляем его к сообщению
                        if file["type"] == "image":
                            message["type"] = "image"
                            message["image"] = {"link": file["link"]}
                        elif file["type"] in ["file", "video", "audio"]:
                            message["type"] = "document"
                            message["document"] = {
                                "link": file["link"],
                                "filename": file["name"],
                            }

                waba.send_message(appinstance, message, line_id=line_id)

            elif connector.service == "waweb":
                try:
                    line = Line.objects.get(line_id=line_id, app_instance=appinstance)
                    wa = Session.objects.get(line=line)
                    if files:
                        for file in files:
                            waweb_tasks.send_message_task.delay(str(wa.session), [chat], file, 'media')
                    else:
                        waweb_tasks.send_message.delay(wa.session, chat, text)
                except Exception as e:
                    raise

            # If OLX connector
            elif connector.service == "olx":
                olx_tasks.send_message.delay(chat, text, files)

        elif event == "ONCRMDEALUPDATE":
            if appinstance.portal.imopenlines_auto_finish:
                deal_id = data.get("data[FIELDS][ID]")
                bitrix_tasks.auto_finish_chat.delay(appinstance.id, deal_id, True)
        
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

        elif event == "ONAPPUNINSTALL":
            portal = appinstance.portal
            appinstance.delete()
            if not AppInstance.objects.filter(portal=portal).exists():
                portal.delete()

    except Exception as e:
        raise
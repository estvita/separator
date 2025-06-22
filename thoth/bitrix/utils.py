import base64
import json
import logging
import re
import uuid
import redis
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.contrib import messages
from django.conf import settings
from django.shortcuts import redirect, get_object_or_404

from rest_framework import status
from rest_framework.response import Response
from django.http import HttpResponse

import thoth.olx.tasks as olx_tasks
import thoth.waba.utils as waba

from thoth.waweb.models import Session
import thoth.waweb.utils as waweb
import thoth.waweb.tasks as waweb_tasks

from .crest import call_method
from .models import App, AppInstance, Bitrix, Line, VerificationCode, Connector
import thoth.bitrix.tasks as bitrix_tasks


redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

THOTH_BITRIX = settings.THOTH_BITRIX

logger = logging.getLogger("django")

GENERAL_EVENTS = [
    "ONAPPUNINSTALL",
]

CONNECTOR_EVENTS = [
    "ONIMCONNECTORMESSAGEADD",
    "ONIMCONNECTORLINEDELETE",
    "ONIMCONNECTORSTATUSDELETE",
]


# Регистрация SMS-провайдера
def messageservice_add(appinstance, phone, line, api_key, service):
    url = appinstance.app.site
    payload = {
        "CODE": f"THOTH_{phone}_{line}",
        "NAME": f"gulin.kz ({phone})",
        "TYPE": "SMS",
        "HANDLER": f"https://{url}/api/bitrix/sms/?api-key={api_key}&service={service}",
    }
    try:
        return call_method(appinstance, "messageservice.sender.add", payload)
    except Exception as e:
        return {"error": str(e)}


def connect_line(request, line_id, entity, connector, redirect_to):
    line_id = str(line_id)
    if line_id.startswith("create__"):
        instance_id = line_id.split("__")[1]
        app_instance = get_object_or_404(AppInstance, id=instance_id, owner=request.user)
        if not app_instance.portal:
            messages.error(request, "Невозможно создать линию: портал не найден")
            return redirect(redirect_to)
        if entity.line:
            call_method(app_instance, "imconnector.activate", {
                "CONNECTOR": connector.code,
                "LINE": entity.line.line_id,
                "ACTIVE": 0,
            })
        if connector.service == "olx":
            line_name = entity.olx_id
        else:
            line_name = entity.phone
        create_payload = {"PARAMS": {"LINE_NAME": line_name}}
        result = call_method(app_instance, "imopenlines.config.add", create_payload)
        if result and result.get("result"):
            new_line_id = result["result"]
            line = Line.objects.create(
                line_id=new_line_id,
                portal=app_instance.portal,
                connector=connector,
                app_instance=app_instance,
                owner=request.user
            )
            entity.line = line
            entity.app_instance = app_instance
            entity.save()

            activate_payload = {
                "CONNECTOR": connector.code,
                "LINE": new_line_id,
                "ACTIVE": 1,
            }
            call_method(app_instance, "imconnector.activate", activate_payload)
            messages.success(request, f"Создана и подключена линия {new_line_id}")
        else:
            messages.error(request, f"Ошибка при создании линии: {result}")
        return redirect(redirect_to)
    else:
        line = get_object_or_404(Line, id=line_id)
        if not line:
            messages.error(request, f"Линия {line_id} не найдена")
            return redirect(redirect_to)
        # Проверка, не занята ли линия другим entity
        entity_model = type(entity)
        usage_count = entity_model.objects.filter(line=line).exclude(pk=entity.pk).count()
        if usage_count > 0:
            messages.error(request, "Эта линия уже используется.")
            return redirect(redirect_to)
        
        app_instance = line.app_instance
        if hasattr(entity, 'sms_service'):
            owner = app_instance.app.owner
            if not hasattr(owner, "auth_token"):
                entity.sms_service = False
                entity.save()
                messages.error(request, f"API key not found for user {owner}. Операция прервана.")
                return redirect(redirect_to)
            api_key = owner.auth_token.key
            phone = re.sub(r'\D', '', entity.phone)
            if entity.sms_service:
                resp = messageservice_add(app_instance, phone, line.line_id, api_key, connector.service)
                if isinstance(resp, dict) and "error" in resp:
                    messages.error(request, f"Ошибка подключения SMS канала: {resp['error']}")
            else:
                try:
                    call_method(app_instance, "messageservice.sender.delete", {"CODE": f"THOTH_{phone}_{line.line_id}"})
                except Exception as e:
                    messages.warning(request, f"Ошибка при удалении SMS канала: {e}")
        
        if entity.line == line:
            messages.success(request, "Выбрана та же линия")
            return redirect(redirect_to)
        if entity.line:
            call_method(app_instance, "imconnector.activate", {
                "CONNECTOR": connector.code,
                "LINE": entity.line.line_id,
                "ACTIVE": 0,
            })
        response = call_method(app_instance, "imconnector.activate", {
            "CONNECTOR": connector.code,
            "LINE": line.line_id,
            "ACTIVE": 1,
        })
        if response.get("result"):
            entity.line = line
            entity.app_instance = app_instance
            entity.save()
            messages.success(request, "Линия подключена")

    messages.success(request, "Настройки обновлены")
    return redirect(redirect_to)


# Подписка на события
def events_bind(events: dict, appinstance: AppInstance, api_key: str):
    url = appinstance.app.site
    for event in events:
        payload = {
            "event": event,
            "HANDLER": f"https://{url}/api/bitrix/?api-key={api_key}",
        }

        bitrix_tasks.call_api.delay(appinstance.id, "event.bind", payload)


def register_connector(appinstance: AppInstance, api_key: str, connector):
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
        events_bind(CONNECTOR_EVENTS, appinstance, api_key)

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
    upload_to_bitrix = call_method(appinstance, "disk.folder.uploadfile", payload)
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
    

def sms_processor(request):
    data = request.data
    application_token = data.get("auth[application_token]")
    appinstance = AppInstance.objects.get(application_token=application_token)
    message_body = data.get("message_body")
    code = data.get("code", {})
    phones = []
    message_to = re.sub(r'\D', '', data.get("message_to"))
    phones.append(message_to)
    line = re.search(r"_(\d+)$", code).group(1)

    status_data = {
        "CODE": code,
        "MESSAGE_ID": data.get("message_id"),
        "STATUS": "delivered"
    }

    bitrix_tasks.call_api.delay(appinstance.id, "messageservice.message.status.update", status_data)

    # начать диалог в открытых линиях
    service = request.query_params.get('service')
    bitrix_tasks.message_add.delay(appinstance.id, line, message_to, message_body, f"thoth_{service}")

    # Messages from SMS gate
    if service == "waba":
        # Проверяем наличие "template+" в начале message_body
        if not message_body.startswith("template+"):
            return Response(
                {"error": "Message body must start with 'template+'"})

        try:
            # Убираем "template+" и разбиваем на три части
            _, template_name, language = message_body.split("+", 2)
        except ValueError as e:
            logger.error(f"Error splitting message_body: {message_body} - {e!s}")
            return Response(
                {"error": "Invalid message body format, expected 'template+name+lang'"})
        message = {
            "type": "template",
            "template": {"name": template_name, "language": {"code": language}},
        }

        waba.send_message(appinstance, message, line, phones)
    
    elif service == "waweb":
        sender  = re.search(r'_(\d+)_', code).group(1)
        try:
            wa = Session.objects.get(phone=sender)
            resp = waweb.send_message(wa.session, message_to, message_body)
            if resp.status_code == 201:
                waweb.store_msg(resp)
        except wa.DoesNotExist:
            raise ValueError(f"No Session found for phone number: {sender}")
        except Exception as e:
            print(f"Failed to send message to {message_to}: {e}")

    return Response({"status": "message processed"}, status=status.HTTP_200_OK)


def event_processor(request):
    try:
        data = request.data
        event = data.get("event")
        domain = data.get("auth[domain]")
        user_id = data.get("auth[user_id]")
        auth_status = data.get("auth[status]")
        access_token = data.get("auth[access_token]")
        refresh_token = data.get("auth[refresh_token]")
        application_token = data.get("auth[application_token]")
        member_id = data.get("auth[member_id]")
        api_key = request.query_params.get("api-key")
        app_id = request.query_params.get("app-id")

        # Проверка наличия приложения в базе данных
        try:
            appinstance = AppInstance.objects.get(application_token=application_token)
            if not appinstance.portal.member_id:
                appinstance.portal.member_id = member_id
                appinstance.portal.save()
            # Обновление токенa от админа
            if access_token and appinstance.portal.user_id == user_id:
                appinstance.access_token = access_token
                appinstance.save()
                if event == "ONAPPINSTALL":
                    return Response({"message": "ok"})

        except AppInstance.DoesNotExist:
            # Если событие ONAPPINSTALL
            if event == "ONAPPINSTALL":
                scope = data.get("auth[scope]", {})
                # Получение приложения по app_id
                try:
                    app = App.objects.get(id=app_id)
                except App.DoesNotExist:
                    return Response({"message": "App not found."})

                try:
                    portal = Bitrix.objects.get(domain=domain)
                except Bitrix.DoesNotExist:
                    portal_data = {
                        "domain": domain,
                        "user_id": user_id,
                        "member_id": member_id,
                        "owner": request.user if auth_status == "L" else None,
                    }
                    portal = Bitrix.objects.create(**portal_data)


                # Определяем владельца для AppInstance
                appinstance_owner = (
                    portal.owner
                    if portal.owner
                    else (request.user if auth_status == "L" else None)
                )

                appinstance_data = {
                    "app": app,
                    "portal": portal,
                    "auth_status": auth_status,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "application_token": application_token,
                    "owner": appinstance_owner,
                }

                appinstance = AppInstance.objects.create(**appinstance_data)

                # Получаем storage_id и сохраняем его
                storage_data = call_method(appinstance, "disk.storage.getforapp", {})
                if "result" in storage_data:
                    storage_id = storage_data["result"]["ID"]
                    appinstance.storage_id = storage_id
                    appinstance.save()

                # Регистрация коннектора/ подписка на события
                def register_events_and_connectors():
                    events_bind(GENERAL_EVENTS, appinstance, api_key)
                    if app.connectors.exists():
                        for connector in app.connectors.all():
                            register_connector(appinstance, api_key, connector)

                transaction.on_commit(register_events_and_connectors)

                # Если портал уже прявязан
                if portal.owner:
                    return Response('App successfully created and linked')
                
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
                    "USER_ID": appinstance.portal.user_id,
                }

                bitrix_tasks.call_api.delay(appinstance.id, "im.notify.system.add", payload)

                # создание лида в битриксе если есть права
                if "user_basic" in scope and THOTH_BITRIX:
                    bitrix_tasks.create_deal.delay(appinstance.id, THOTH_BITRIX, app.name)

                return Response(
                    {"message": "App and portal successfully created and linked."},
                    status=status.HTTP_201_CREATED,
                )
            else:
                return Response({"message": "App not found and not an install event."})

        # Обработка события ONIMCONNECTORMESSAGEADD
        if event == "ONIMCONNECTORMESSAGEADD":
            connector_code = data.get("data[CONNECTOR]")
            connector = get_object_or_404(Connector, code=connector_code)
            if not connector:
                return Response({'Connector not found'})
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

            bitrix_tasks.call_api.delay(appinstance.id, "imconnector.send.status.delivery", status_data)

            # Проверяем наличие сообщения в редис (отправлено из других сервисов )
            if redis_client.exists(f'bitrix:{domain}:{message_id}'):
                return Response({'message': 'loop message'})
            
            file_type = data.get("data[MESSAGES][0][message][files][0][type]", None)
            text = data.get("data[MESSAGES][0][message][text]", None)
            if text:
                text = re.sub(r"\[(?!(br|\n))[^\]]+\]", "", text)
                text = text.replace("[br]", "\n")

            files = []
            if file_type:
                files = extract_files(data)

            # If WABA connector
            if connector.service == "waba":

                message = {
                    "biz_opaque_callback_data": f"{line_id}_{chat_id}_{message_id}",
                }

                if not files and text:
                    message["type"] = "text"
                    message["text"] = {"body": text}

                # Обработка шаблонных сообщений
                if "template-" in text:
                    _, template_body = text.split("-")
                    template, language = template_body.split("+")
                    message["type"] = "template"
                    message["template"] = {
                        "name": template,
                        "language": {"code": language},
                    }

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

                resp = waba.send_message(appinstance, message, line_id, chat, user_id)

                if resp.status_code != 200:
                    error = resp.json()
                    if "error" in error:
                        error = error.get("error")
                        message = error.get("message")
                        details = None
                        error_data = error.get("error_data", {})
                        if error_data:
                            details = error_data.get("details")
                        payload = {
                            "message": f"{message} {details}",
                            "USER_ID": user_id
                        }
                        resp = call_method(appinstance, "im.notify.system.add", payload)

            elif connector.service == "waweb":
                try:
                    line = Line.objects.get(line_id=line_id, app_instance=appinstance)
                    wa = Session.objects.get(line=line)
                    if files:
                        for file in files:
                            waweb_tasks.send_message_task.delay(str(wa.session), [chat], file, 'media')
                    resp = waweb.send_message(wa.session, chat, text)
                    if resp.status_code == 201:
                        waweb.store_msg(resp)
                except Exception as e:
                    print(f'Failed to send waweb message: {str(e)}')
                    return Response({'error': f'Failed to send message: {str(e)}'})

            # If OLX connector
            elif connector.service == "olx":
                olx_tasks.send_message.delay(chat, text, files)

            return Response(
                {"status": "ONIMCONNECTORMESSAGEADD event processed"},
                status=status.HTTP_200_OK,
            )

        elif event == "ONIMCONNECTORSTATUSDELETE":
            line_id = data.get("data[line]")
            connector_code = data.get("data[connector]")
            connector = get_object_or_404(Connector, code=connector_code)
            if not connector:
                return Response({'Connector not found'})
            try:
                line = Line.objects.get(line_id=line_id, app_instance=appinstance)

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

                return Response("Line disconnected")

            except Line.DoesNotExist:
                return Response(
                    {"status": "Line not found"},
                    status=status.HTTP_200_OK,
                )


        elif event == "ONIMCONNECTORLINEDELETE":
            line_id = data.get("data")
            try:
                line = Line.objects.filter(line_id=line_id, app_instance=appinstance).first()
                if line:
                    line.delete()
                return Response({"status": "Line deleted"}, status=status.HTTP_200_OK)
            except Line.DoesNotExist:
                return Response(
                    {"status": "Line not found"}, status=status.HTTP_200_OK
                )

        elif event == "ONAPPUNINSTALL":
            portal = appinstance.portal
            appinstance.delete()
            if not AppInstance.objects.filter(portal=portal).exists():
                portal.delete()
                return Response(f"{appinstance} and associated portal deleted")
            else:
                return Response(f"{appinstance} deleted")

        else:
            return Response({"message": "Unsupported event"})

    except Exception as e:
        logger.error(f"Error occurred: {e!s}")
        return Response(
            {"error": "Internal server error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

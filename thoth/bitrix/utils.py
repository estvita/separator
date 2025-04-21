import base64
import json
import logging
import os
import re
import uuid
import redis
from datetime import timedelta
from django.utils import timezone
from django.conf import settings

from rest_framework import status
from rest_framework.response import Response

import thoth.olx.tasks as olx_tasks
import thoth.waba.utils as waba
from thoth.olx.models import OlxUser
from thoth.waba.models import Phone

from thoth.waweb.models import WaSession
import thoth.waweb.utils as waweb
import thoth.waweb.tasks as waweb_tasks

from .crest import call_method
from .models import App, AppInstance, Bitrix, Line, VerificationCode
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

ALL_EVENTS = GENERAL_EVENTS + CONNECTOR_EVENTS


def thoth_logo(connetor):
    dir = os.path.dirname(os.path.abspath(__file__))
    image = os.path.join(dir, "img", f"{connetor}.svg")

    with open(image, "rb") as file:
        image_data = file.read()
        encoded_image = base64.b64encode(image_data).decode("utf-8")
        return f"data:image/svg+xml;base64,{encoded_image}"


# Регистрация коннектора
def register_connector(appinstance: AppInstance, api_key: str):
    connetor = appinstance.app.name
    url = appinstance.app.site
    payload = {
        "ID": f"thoth_{connetor}",
        "NAME": f"THOTH {connetor.upper()}",
        "ICON": {
            "DATA_IMAGE": thoth_logo(connetor),
        },
        "PLACEMENT_HANDLER": f"https://{url}/api/bitrix/placement/?api-key={api_key}&inst={appinstance.id}",
    }

    call_method(appinstance, "imconnector.register", payload)

# Подписка на события
def events_bind(events: dict, appinstance: AppInstance, api_key: str):
    url = appinstance.app.site
    for event in events:
        payload = {
            "event": event,
            "HANDLER": f"https://{url}/api/bitrix/?api-key={api_key}",
        }

        call_method(appinstance, "event.bind", payload)


# Регистрация SMS-провайдера
def messageservice_add(appinstance, phone, line, api_key, service):
    url = appinstance.app.site
    filtered_text = ''.join(filter(str.isalnum, phone))
    payload = {
        "CODE": f"THOTH_{filtered_text}_{line}",
        "NAME": f"gulin.kz ({phone})",
        "TYPE": "SMS",
        "HANDLER": f"https://{url}/api/bitrix/sms/?api-key={api_key}&service={service}",
    }

    return call_method(appinstance, "messageservice.sender.add", payload)


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

def get_line(app_instance, line_id):
    line_data = call_method(
        app_instance, "imopenlines.config.get", {"CONFIG_ID": line_id}
    )
    if "result" not in line_data:
        return Response({"error": f"{line_data}"})
    line_name = line_data["result"]["LINE_NAME"]
    return line_name


def process_placement(request):
    try:
        data = request.data
        placement_options = data.get("PLACEMENT_OPTIONS", {})
        inst = request.query_params.get("inst", {})

        placement_options = json.loads(placement_options)
        line_id = placement_options.get("LINE")
        connector = placement_options.get("CONNECTOR")

        app_instance = AppInstance.objects.get(id=inst)

        line_name = get_line(app_instance, line_id)

        try:
            line = Line.objects.get(line_id=line_id, app_instance=app_instance)
            if connector == "thoth_olx":
                finded_object = OlxUser.objects.filter(line=line).first()
            elif connector == "thoth_waba":
                finded_object = Phone.objects.filter(line=line).first()
            elif connector == "thoth_waweb":
                finded_object = WaSession.objects.filter(line=line).first()
            
            if finded_object:
                return Response("Ничего не изменилось, спасибо.")

            else:
                if connector == "thoth_olx":
                    olxuser = OlxUser.objects.get(olx_id=line_name)
                    olxuser.line = line
                    olxuser.save()

                elif connector == "thoth_waba":
                    phone = Phone.objects.get(phone=line_name)
                    phone.line = line
                    phone.save()

                elif connector == "thoth_waweb":
                    phone = WaSession.objects.get(phone=line_name)
                    phone.line = line
                    phone.save()

                payload = {
                    "CONNECTOR": connector,
                    "LINE": line_id,
                    "ACTIVE": 1,
                }
                call_method(app_instance, "imconnector.activate", payload)
                return Response("Линия подключена, спасибо.")

        except Line.DoesNotExist:
            return Response("Для изменения линии, удалите текущую в интерфейса CRM, а затем заоново подключите аккаунт на портале gulin.kz.")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return Response(
            {"error": "An unexpected error occurred"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


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

    bitrix_tasks.call_api.delay(application_token, "messageservice.message.status.update", status_data)

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
            wa = WaSession.objects.get(phone=sender)
            resp = waweb.send_message(wa.session, message_to, message_body)
            if resp.status_code == 201:
                waweb.store_msg(resp)
        except wa.DoesNotExist:
            raise ValueError(f"No WaSession found for phone number: {sender}")
        except Exception as e:
            print(f"Failed to send message to {message_to}: {e}")

    return Response({"status": "message processed"}, status=status.HTTP_200_OK)


def event_processor(request):
    try:
        data = request.data
        event = data.get("event", {})
        domain = data.get("auth[domain]", {})
        user_id = data.get("auth[user_id]", {})
        auth_status = data.get("auth[status]", {})
        client_endpoint = data.get("auth[client_endpoint]", {})
        access_token = data.get("auth[access_token]", {})
        refresh_token = data.get("auth[refresh_token]", {})
        application_token = data.get("auth[application_token]", {})
        api_key = request.query_params.get("api-key", {})
        app_id = request.query_params.get("app-id", {})

        # Проверка наличия приложения в базе данных
        try:
            appinstance = AppInstance.objects.get(application_token=application_token)
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
                    return Response(
                        {"error": "App not found."}, status=status.HTTP_404_NOT_FOUND
                    )

                try:
                    portal = Bitrix.objects.get(domain=domain)
                except Bitrix.DoesNotExist:
                    portal_data = {
                        "domain": domain,
                        "user_id": user_id,
                        "client_endpoint": client_endpoint,
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
                storage_id_data = call_method(appinstance, "disk.storage.getforapp", {})
                storage_id = storage_id_data["result"]["ID"]
                appinstance.storage_id = storage_id
                appinstance.save()

                # Регистрация коннектора/ подписка на события
                if app.connector:
                    register_connector(appinstance, api_key)
                    events_bind(ALL_EVENTS, appinstance, api_key)
                else:
                    events_bind(GENERAL_EVENTS, appinstance, api_key)

                # Если портал уже прявязан
                if portal.owner:
                    return Response('App successfully created and linked')

                code = uuid.uuid4()
                VerificationCode.objects.create(
                    portal=portal,
                    code=code,
                    expires_at=timezone.now() + timedelta(days=1),
                )

                payload = {
                    "message": f"Ваш код подтверждения: {code}. Введите его на странице https://{appinstance.app.site}/portals/",
                    "USER_ID": appinstance.portal.user_id,
                }

                call_method(appinstance, "im.notify.system.add", payload)

                # создание лида в битриксе если есть права
                if "user_basic" in scope and THOTH_BITRIX:
                    bitrix_tasks.create_deal.delay(appinstance.id, THOTH_BITRIX, app.name)

                return Response(
                    {"message": "App and portal successfully created and linked."},
                    status=status.HTTP_201_CREATED,
                )
            else:
                return Response(
                    {"error": "App not found and not an install event."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Обработка события ONIMCONNECTORMESSAGEADD
        if event == "ONIMCONNECTORMESSAGEADD":
            connector = data.get("data[CONNECTOR]")
            line_id = data.get("data[LINE]")
            message_id = data.get("data[MESSAGES][0][im][message_id]")
            chat_id = data.get("data[MESSAGES][0][im][chat_id]")
            chat = data.get("data[MESSAGES][0][chat][id]")
            status_data = {
                "CONNECTOR": connector,
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

            bitrix_tasks.call_api.delay(application_token, "imconnector.send.status.delivery", status_data)

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
            if connector == "thoth_waba":

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

            elif connector == "thoth_waweb":
                try:
                    line = Line.objects.get(line_id=line_id, app_instance=appinstance)
                    wa = WaSession.objects.get(line=line)
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
            elif connector == "thoth_olx":
                olx_tasks.send_message.delay(chat, text, files)


            return Response(
                {"status": "ONIMCONNECTORMESSAGEADD event processed"},
                status=status.HTTP_200_OK,
            )

        elif event == "ONIMCONNECTORSTATUSDELETE":
            line_id = data.get("data[line]")
            connector = data.get("data[connector]")
            try:
                line = Line.objects.get(line_id=line_id, app_instance=appinstance)

                if connector == "thoth_olx":
                    olxuser = line.olx_users.first()
                    if olxuser:
                        olxuser.line = None
                        olxuser.save()

                elif connector == "thoth_waba":
                    phone = line.phones.first()
                    if phone:
                        phone.line = None
                        phone.save()
                
                elif connector == "thoth_waweb":
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
                line = Line.objects.filter(
                    line_id=line_id, app_instance=appinstance
                ).first()
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
            return Response(
                {"error": "Unsupported event"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    except Exception as e:
        logger.error(f"Error occurred: {e!s}")
        return Response(
            {"error": "Internal server error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

import base64
import json
import logging
import os
import re
import uuid
from datetime import timedelta
from django.utils import timezone

from rest_framework import status
from rest_framework.response import Response

import thoth.olx.utills
import thoth.waba.utils
from thoth.olx.models import OlxUser
from thoth.waba.models import Phone

from .crest import call_method
from .models import App, AppInstance, Bitrix, Line, VerificationCode


logger = logging.getLogger("django")

EVENTS = [
    "ONIMCONNECTORMESSAGEADD",
    "ONIMCONNECTORLINEDELETE",
    "ONIMCONNECTORSTATUSDELETE",
    "ONAPPUNINSTALL",
]


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
    for event in EVENTS:
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
        "NAME": f"THOTH ({phone})",
        "TYPE": "SMS",
        "HANDLER": f"https://{url}/api/bitrix/sms/?api-key={api_key}&service={service}",
    }

    return call_method(appinstance, "messageservice.sender.add", payload)


def get_personal_mobile(users):
    personal_mobiles = []
    for user_id, user_info in users.items():
        if user_info.get("external_auth_id") == "imconnector":
            phones = user_info.get("phones", {})
            personal_mobile = phones.get("personal_mobile")
            if personal_mobile:
                personal_mobiles.append(personal_mobile)
    return personal_mobiles


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

                payload = {
                    "CONNECTOR": connector,
                    "LINE": line_id,
                    "ACTIVE": 1,
                }
                call_method(app_instance, "imconnector.activate", payload)
                return Response("Линия подключена, спасибо.")

        except Line.DoesNotExist:
            return Response("Создать линию можно на app.thoth.kz, спасибо.")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return Response(
            {"error": "An unexpected error occurred"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def sms_processor(request):
    data = request.data
    application_token = data.get("auth[application_token]", {})
    appinstance = AppInstance.objects.get(application_token=application_token)
    domain = data.get("auth[domain]", {})
    sms_msg = data.get("type", {})
    message_body = data.get("message_body", {})

    # Messages from SMS gate
    if sms_msg == "SMS":
        phones = []
        message_to = data.get("message_to", {})
        phones.append(message_to)

        try:
            template, language = message_body.split("+")
        except ValueError as e:
            logger.error(f"Error splitting message_body: {message_body} - {e!s}")
            return Response(
                {"error": "Invalid message body content"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        message = {
            "type": "template",
            "template": {"name": template, "language": {"code": language}},
        }
        code = data.get("code", {})
        line = re.search(r"_(\d+)$", code).group(1)

        thoth.waba.utils.send_message(appinstance, message, line, phones)
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

        except AppInstance.DoesNotExist:
            # Если событие ONAPPINSTALL
            if event == "ONAPPINSTALL":
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

                # Регистрируем коннектор
                register_connector(appinstance, api_key)

                # Если тиражное приложение отправлем код
                if auth_status == "F":
                    code = uuid.uuid4()
                    VerificationCode.objects.create(
                        portal=portal,
                        code=code,
                        expires_at=timezone.now() + timedelta(days=1),
                    )

                    payload = {
                        "message": f"Ваш код подтверждения: {code}",
                        "USER_ID": appinstance.portal.user_id,
                    }

                    call_method(appinstance, "im.notify.system.add", payload)

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
            line = data.get("data[LINE]", {})
            message_id = data.get("data[MESSAGES][0][im][message_id]")
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
                chat_id = data.get("data[MESSAGES][0][im][chat_id]")
                message = {
                    "biz_opaque_callback_data": f"{line}_{chat_id}_{message_id}",
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

                # Получаем список пользователей и номера телефонов
                user_list = call_method(
                    appinstance, "im.chat.user.list", {"CHAT_ID": chat_id}
                )
                if user_list:
                    users = call_method(
                        appinstance, "im.user.list.get", {"ID": user_list["result"]}
                    )
                    phones = get_personal_mobile(users["result"])

                    # Если есть файлы, отправляем сообщение с каждым файлом отдельно
                    if files:
                        for file in files:
                            file_message = message.copy()

                            # Определяем тип файла и добавляем его к сообщению
                            if file["type"] == "image":
                                file_message["type"] = "image"
                                file_message["image"] = {"link": file["link"]}
                            elif file["type"] in ["file", "video", "audio"]:
                                file_message["type"] = "document"
                                file_message["document"] = {
                                    "link": file["link"],
                                    "filename": file["name"],
                                }

                            thoth.waba.utils.send_message(
                                appinstance, file_message, line, phones
                            )
                    else:
                        # Если файлов нет, отправляем только текстовое сообщение
                        thoth.waba.utils.send_message(
                            appinstance, message, line, phones
                        )

            # If OLX connector
            elif connector == "thoth_olx":
                chat_id = data.get("data[MESSAGES][0][chat][id]")
                resp = thoth.olx.utills.send_message(chat_id, text, files)
                if resp.status_code == 200:
                    payload = {
                        "CONNECTOR": "thoth_olx",
                        "LINE": line,
                        "MESSAGES": [
                            {
                                "im": {
                                    "chat_id": chat_id,
                                    "message_id": message_id,
                                },
                            },
                        ],
                    }

                    call_method(
                        appinstance, "imconnector.send.status.delivery", payload
                    )

                else:
                    error_text = resp.json()["error"]["detail"]
                    _, _, interlocutor_id = chat_id.split("-")

                    payload = {
                        "CONNECTOR": "thoth_olx",
                        "LINE": line,
                        "MESSAGES": [
                            {
                                "user": {
                                    "id": interlocutor_id,
                                },
                                "chat": {
                                    "id": chat_id,
                                },
                                "message": {
                                    "text": f"This is a response from the OLX server: {error_text}",
                                },
                            },
                        ],
                    }

                    call_method(appinstance, "imconnector.send.messages", payload)

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

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

from rest_framework import status
from rest_framework.response import Response
from django.http import HttpResponse

import thoth.olx.tasks as olx_tasks
import thoth.waba.utils as waba

from thoth.waweb.models import Session
import thoth.waweb.tasks as waweb_tasks

from .crest import call_method
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
            portal = Bitrix.objects.filter(member_id=member_id, owner=request.user).first()
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
            call_method(app_instance, "imconnector.activate", {
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
        result = call_method(app_instance, "imopenlines.config.add", create_payload)
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
            call_method(app_instance, "imconnector.activate", activate_payload)
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


# Подписка на события
def events_bind(appinstance: AppInstance, api_key: str):
    url = appinstance.app.site
    for event in appinstance.app.events.strip().splitlines():
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
    upload_to_bitrix = call_method(appinstance, "disk.storage.uploadfile", payload)
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
    app_instance = AppInstance.objects.filter(application_token=application_token).first()
    if not app_instance:
        return HttpResponse("app not found")
    message_body = data.get("message_body")
    code = data.get("code", {})
    sender = code.split('_')[-1]
    phones = []
    message_to = re.sub(r'\D', '', data.get("message_to"))
    phones.append(message_to)

    status_data = {
        "CODE": code,
        "MESSAGE_ID": data.get("message_id"),
        "STATUS": "delivered"
    }

    bitrix_tasks.call_api(app_instance.id, "messageservice.message.status.update", status_data)

    # начать диалог в открытых линиях
    service = request.query_params.get('service')
    line = None

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

        # waba.send_message(appinstance, message, line, phones)
    
    elif service == "waweb":
        try:
            wa = Session.objects.get(phone=sender)
            line = wa.line
            waweb_tasks.send_message(wa.session, message_to, message_body)
        except wa.DoesNotExist:
            raise ValueError(f"No Session found for phone number: {code}")
        except Exception as e:
            print(f"Failed to send message to {message_to}: {e}")

    if line:
        bitrix_tasks.message_add(app_instance.id, line.line_id, message_to, message_body, line.connector.code)
    return Response({"status": "message processed"}, status=status.HTTP_200_OK)


def event_processor(request):
    try:
        data = request.data
        event = data.get("event").upper()
        domain = data.get("auth[domain]")
        user_id = data.get("auth[user_id]")
        auth_status = data.get("auth[status]")
        access_token = data.get("auth[access_token]")
        refresh_token = data.get("auth[refresh_token]")
        application_token = data.get("auth[application_token]")
        member_id = data.get("auth[member_id]")
        api_key = request.query_params.get("api-key")

        # Проверка наличия установки
        try:
            appinstance = AppInstance.objects.get(application_token=application_token)

        except AppInstance.DoesNotExist:
            if event == "ONAPPINSTALL":                
                
                app_id = request.query_params.get("app-id")
                # Получение приложения по app_id
                try:
                    app = App.objects.get(id=app_id)
                except App.DoesNotExist:
                    return Response({"message": "App not found."})
                
                owner_user = request.user if auth_status == "L" else None
               
                portal, created = Bitrix.objects.get_or_create(
                    member_id=member_id,
                    defaults={
                        "domain": domain,
                        "owner": owner_user,
                    }
                )
                # Определяем владельца для AppInstance
                appinstance_owner = (
                    portal.owner
                    if portal.owner
                    else owner_user
                )

                appinstance_data = {
                    "app": app,
                    "portal": portal,
                    "auth_status": auth_status,
                    "application_token": application_token,
                    "owner": appinstance_owner,
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
                storage_data = call_method(appinstance, "disk.storage.getforapp", {})
                if "result" in storage_data:
                    storage_id = storage_data["result"]["ID"]
                    appinstance.storage_id = storage_id
                    appinstance.save()

                # Регистрация коннектора/ подписка на события
                def register_events_and_connectors():
                    events_bind(appinstance, api_key)
                    if app.connectors.exists():
                        for connector in app.connectors.all():
                            register_connector(appinstance, api_key, connector)

                transaction.on_commit(register_events_and_connectors)

                if settings.ASTERX_SERVER and app.asterx:
                    from thoth.asterx.views import get_portal_settings
                    get_portal_settings(member_id)

                if VENDOR_BITRIX_INSTANCE:
                    bitrix_tasks.create_deal.delay(appinstance.id, VENDOR_BITRIX_INSTANCE, app.name)

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
                    "USER_ID": user_id,
                }

                bitrix_tasks.call_api.delay(appinstance.id, "im.notify.system.add", payload)

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

            bitrix_tasks.call_api(appinstance.id, "imconnector.send.status.delivery", status_data)

            # Проверяем наличие сообщения в редис (отправлено из других сервисов )
            for _ in range(5):
                if redis_client.exists(f'bitrix:{member_id}:{message_id}'):
                    return Response({'message': 'loop message'})
                time.sleep(1)
            
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

                resp = waba.send_message(appinstance, message, line_id, chat)

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
                            waweb_tasks.send_message_task(str(wa.session), [chat], file, 'media')
                    else:
                        waweb_tasks.send_message(wa.session, chat, text)
                except Exception as e:
                    print(f'Failed to send waweb message: {str(e)}')
                    return Response({'error': f'Failed to send message: {str(e)}'})

            # If OLX connector
            elif connector.service == "olx":
                olx_tasks.send_message(chat, text, files)

            return Response(
                {"status": "ONIMCONNECTORMESSAGEADD event processed"},
                status=status.HTTP_200_OK,
            )

        elif event == "ONCRMDEALUPDATE":
            if appinstance.portal.imopenlines_auto_finish:
                deal_id = data.get("data[FIELDS][ID]")
                deal_data = call_method(appinstance, "crm.deal.get", {"ID": deal_id}, admin=True)
                if "result" in deal_data:
                    deal_data = deal_data["result"]
                    if deal_data.get("CLOSED") == "Y":
                        # Добавляем задержку в секундах (finish_delay в минутах * 60)
                        delay_seconds = appinstance.portal.finish_delay * 60
                        bitrix_tasks.auto_finish_chat.apply_async(
                            args=[appinstance.id, deal_id], 
                            countdown=delay_seconds
                        )
            return Response('event processed')
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
                print(f'Failed to send event: {str(e)}')
            return Response('event processed')
        
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
                print(f'Failed to send event: {str(e)}')
            return Response('event processed')

        elif event == "ONAPPUNINSTALL":
            portal = appinstance.portal
            appinstance.delete()
            if not AppInstance.objects.filter(portal=portal).exists():
                portal.delete()
                return Response(f"{appinstance} and associated portal deleted")
            else:
                return Response(f"{appinstance} deleted")

        else:
            return Response('Unsupported event')

    except Exception as e:
        logger.error(f"Error occurred: {e!s}")
        return Response(
            {"error": "Internal server error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
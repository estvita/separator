import re
import redis
from celery import shared_task
import logging
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .crest import call_method, refresh_token
from .models import AppInstance, Credential

from separator.waba.models import Phone
from separator.waweb.models import Session
from separator.users.models import User

logger = logging.getLogger("django")

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def call_api(self, id, method, payload, b24_user=None):
    try:
        app_instance = AppInstance.objects.get(id=id)
        resp = call_method(app_instance, method, payload, b24_user_id=b24_user)
        return resp
    except (ObjectDoesNotExist, Exception) as exc:
        raise self.retry(exc=exc)

@shared_task(queue='bitrix')
def upd_refresh_token(period):
    now = timezone.now()
    credentials = Credential.objects.all()
    for credential in credentials:
        need_refresh = (
            credential.refresh_date is None or
            credential.refresh_date < now - timedelta(days=period)
        )
        if need_refresh:
            refresh_token(credential)


# Регистрация SMS-провайдера
@shared_task(queue='bitrix')
def messageservice_add(app_instance_id, entity_id, service):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
        if service == "waweb":
            entity = Session.objects.get(id=entity_id)
        elif service == "waba":
            entity = Phone.objects.get(id=entity_id)
        if hasattr(entity, 'sms_service'):
            owner = app_instance.app.owner
            if not hasattr(owner, "auth_token"):
                entity.sms_service = False
                entity.save()
                raise Exception("Owner has no auth_token!")

            phone = re.sub(r'\D', '', entity.phone)
            if entity.sms_service:
                try:
                    all_providers = call_method(app_instance, "messageservice.sender.list", admin=True)
                except Exception as e:
                    raise Exception(f"list providers fail: {e}")

                if "result" in all_providers and code in all_providers.get("result"):
                    raise Exception(f"{code} already exists")

                url = app_instance.app.site.domain
                code = f"{url}_{phone}"
                payload = {
                    "CODE": code,
                    "NAME": code,
                    "TYPE": "SMS",
                    "HANDLER": f"https://{url}/api/bitrix/sms/?service={service}",
                }
                return call_method(app_instance, "messageservice.sender.add", payload, admin=True)
            else:
                return call_method(app_instance, "messageservice.sender.delete", {"CODE": code}, admin=True)

        else:
            raise Exception("Entity has no sms_service attribute!")
    except Exception as e:
        raise


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def send_messages(self, app_instance_id, user_phone, text, connector,
                  line, sms=False, pushName=None,
                  message_id=None, attachments=None, profilepic_url=None,
                  chat_id=None, chat_url=None, user_id=None):
    init_message = "System: initiation message."
    if pushName:
        pushName = f"{user_phone} ({pushName})"
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
        bitrix_msg = {
            "CONNECTOR": connector,
            "LINE": line,
            "MESSAGES": [
                {
                    "user": {
                        "phone": user_phone,
                        "name": pushName or user_phone,
                        "id": user_id or user_phone,
                        "skip_phone_validate": 'Y',
                        "picture": {
                            "url": profilepic_url
                        }
                    },
                    "chat": {
                        "id": chat_id or user_phone,
                        "url": chat_url
                    },
                    "message": {
                        "text": init_message if sms else text,
                        "id": message_id,
                        "files": attachments
                    }
                }
            ],
        }

        resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg)

        result = resp.get("result", {})
        results = result.get("DATA", {}).get("RESULT", [])
        for result_item in results:
            chat_session = result_item.get("session", {})
            if chat_session:
                member_id = app_instance.portal.member_id
                chat_id = chat_session.get("CHAT_ID")
                identity = user_id or user_phone
                redis_client.set(f"bitrix_chat:{member_id}:{line}:{identity}", chat_id)
                if sms:
                    resp = message_add(app_instance_id, line, user_phone, text, connector)
        return resp

    except Exception as e:
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def message_add(self, app_instance_id, line_id, user_phone, text, connector, attach=None):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
    except AppInstance.DoesNotExist:
        logger.error(f"AppInstance {app_instance_id} does not exist")
        raise

    member_id = app_instance.portal.member_id
    chat_key = f'bitrix_chat:{member_id}:{line_id}:{user_phone}'

    if redis_client.exists(chat_key):
        chat_id = redis_client.get(chat_key).decode('utf-8')
        payload = {
            "DIALOG_ID": f"chat{chat_id}",
            "MESSAGE": text,
            "SYSTEM": "Y",
            "ATTACH": attach
        }
        max_send_attempts = 3

        for attempt in range(max_send_attempts):
            try:
                resp = call_method(app_instance, "im.message.add", payload)
                message_id = resp.get("result")
                redis_client.setex(f'bitrix:{member_id}:{message_id}', 600, message_id)
                payload_status = {
                    "CONNECTOR": connector,
                    "LINE": line_id,
                    "MESSAGES": [{
                        "im": {
                            "chat_id": chat_id,
                            "message_id": message_id
                        }
                    }]
                }
                return call_method(app_instance, "imconnector.send.status.delivery", payload_status)
            except Exception as e:
                if attempt >= max_send_attempts - 1:
                    logger.error(f"Exception occurred while sending message: {e}")
                    raise
                else:
                    self.retry(exc=e)
    else:
        return send_messages(app_instance_id, user_phone, text, connector, line_id, True)


@shared_task(queue='bitrix')
def prepare_lead(user_id, lead_title):
    user = User.objects.filter(id=user_id).first()
    if not user:
        raise Exception("user not found")
    site = user.site
    if not site:
        raise Exception(f"site for {user.email} not found")
    if not site.profile:
        raise Exception(f"site.profile for {site.domain} not found")
    owner = site.profile.owner
    vendor_instance = AppInstance.objects.filter(owner=owner, app__vendor=True).first()
    if not vendor_instance:
        raise Exception(f"vendor instance for vendor {owner.email} not found")
    payload = {
        "FILTER": {
            "LOGIC": "OR",
            "EMAIL": user.email,
            "PHONE": str(user.phone_number)
        },
        "entityTypeId": 3, #contacts
    }
    contact_id = None
    client_data = call_method(vendor_instance, "crm.item.list", payload)
    if "result" in client_data:
        client_data = client_data.get("result", {}).get("items", [])
        if client_data:
            contact_id = client_data[0].get("id")
    if not contact_id:
        # create contact
        contact_data = {
            "fields": {
                "NAME": user.name,
                "EMAIL": [
                    {
                        "VALUE": user.email,
                        "VALUE_TYPE": "WORK"
                    }
                ],
                "PHONE": [
                    {
                        "VALUE": str(user.phone_number),
                        "VALUE_TYPE": "MOBILE"
                    }
                ]
            }
        }

        create_contact = call_method(vendor_instance, "crm.contact.add", contact_data)
        if "result" in create_contact:
            contact_id = create_contact.get("result")
    if contact_id:
        lead_data = {
            "fields": {
                "TITLE": lead_title,
                "CONTACT_ID": contact_id,
                "OPENED": "N",
            }
        }
        call_method(vendor_instance, "crm.lead.add", lead_data)


@shared_task(queue='chat_finish')
def auto_finish_chat(instance_id, deal_id, init=False):
    try:
        app_instance = AppInstance.objects.get(id=instance_id)
        payload = {
            "filter": {
                "ID": deal_id
            },
            "select": ["CLOSED"]
        }
        deal_data = call_method(app_instance, "crm.deal.list", payload, admin=True)
        deal_list = deal_data.get("result", [])
        deal = next((d for d in deal_list if str(d.get("ID")) == str(deal_id)), None)

        if not deal:
            raise Exception(f"Deal {deal_id} not found")

        if not init:
            if deal.get("CLOSED") == "Y":
                payload = {
                    "CRM_ENTITY_TYPE": "DEAL",
                    "CRM_ENTITY": deal_id
                }
                chat_data = call_method(app_instance, "imopenlines.crm.chat.getLastId", payload, admin=True)
                if "result" in chat_data:
                    chat_id = chat_data.get("result")
                    return call_method(app_instance, "imopenlines.operator.another.finish", {"CHAT_ID": chat_id}, admin=True)
                else:
                    raise Exception(f"chat not found: {chat_data}")
            else:
                raise Exception(f"Deal {deal_id} is not closed yet")
        else:
            if deal.get("CLOSED") == "Y":
                delay_seconds = app_instance.portal.finish_delay * 60
                auto_finish_chat.apply_async(
                    args=[app_instance.id, deal_id],
                    countdown=delay_seconds
                )
    except Exception as e:
        raise

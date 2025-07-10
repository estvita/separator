import redis
from celery import shared_task
import logging
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .crest import call_method, refresh_token
from .models import AppInstance, Credential


logger = logging.getLogger("django")

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

FROM_MARKET_FIELD = settings.FROM_MARKET_FIELD

@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def call_api(self, id, method, payload):
    try:
        appinstance = AppInstance.objects.get(id=id)
        return call_method(appinstance, method, payload)
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
        if need_refresh and not credential.app_instance.portal.license_expired:
            refresh_token(credential)


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def send_messages(self, app_instance_id, user_phone, text, connector,
                  line, sms=False, pushName=None,
                  message_id=None, attachments=None, profilepic_url=None,
                  chat_id=None, chat_url=None, user_id=None):
    init_message = "System: initiation message."
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
                domain = app_instance.portal.domain
                chat_id = chat_session.get("CHAT_ID")
                identity = user_id or user_phone
                redis_client.set(f"bitrix_chat:{domain}:{line}:{identity}", chat_id)
                if sms:
                    message_add.delay(app_instance_id, line, user_phone, text, connector)
        return resp

    except Exception as e:
        raise self.retry(exc=e)



@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def message_add(self, app_instance_id, line_id, user_phone, text, connector):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
    except AppInstance.DoesNotExist:
        logger.error(f"AppInstance {app_instance_id} does not exist")
        raise

    domain = app_instance.portal.domain
    chat_key = f'bitrix_chat:{domain}:{line_id}:{user_phone}'

    if redis_client.exists(chat_key):
        chat_id = redis_client.get(chat_key).decode('utf-8')
        payload = {
            "DIALOG_ID": f"chat{chat_id}",
            "MESSAGE": text,
            "SYSTEM": "Y"
        }

        max_send_attempts = 3

        for attempt in range(max_send_attempts):
            try:
                resp = call_method(app_instance, "im.message.add", payload)
                message_id = resp.get("result")
                redis_client.setex(f'bitrix:{domain}:{message_id}', 600, message_id)
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
                call_method(app_instance, "imconnector.send.status.delivery", payload_status)
                return resp
            except Exception as e:
                if attempt >= max_send_attempts - 1:
                    logger.error(f"Exception occurred while sending message: {e}")
                    raise
                else:
                    self.retry(exc=e)

    send_messages.delay(app_instance_id, user_phone, text, connector, line_id, True)


@shared_task(queue='bitrix')
def create_deal(app_instance_id, vendor_inst_id, app_name):
    app_instance = AppInstance.objects.get(id=app_instance_id)
    try:
        user_current = call_method(app_instance, "user.current", {})
        user_data = user_current.get("result", {})
        user_email = user_data.get("EMAIL")
    except Exception as e:
        return
    if not user_email:
        return
    user_id = None
    venrot_instance = AppInstance.objects.get(id=vendor_inst_id)
    # Поиск контакта в битрикс 
    payload = {
        "FILTER": {
            "EMAIL": user_email
        },
        "select": [FROM_MARKET_FIELD]
    }
    client_data = call_method(venrot_instance, "crm.contact.list", payload)
    if "result" in client_data:
        client_data = client_data.get("result", [])
        if client_data:
            from_market = client_data[0].get(FROM_MARKET_FIELD)
            if from_market == "1":
                return
            user_id = client_data[0].get("ID")
    if not user_id:        
        contact_data = {
            "fields": {
                "NAME": user_data.get("NAME"),
                "LAST_NAME": user_data.get("LAST_NAME"),
                FROM_MARKET_FIELD: "1",
                "EMAIL": [
                    {
                        "VALUE": user_email,
                        "VALUE_TYPE": "WORK"
                    }
                ],
                "PHONE": [
                    {
                        "VALUE": user_data.get("WORK_PHONE"),
                        "VALUE_TYPE": "WORK"
                    },
                    {
                        "VALUE": user_data.get("PERSONAL_MOBILE"),
                        "VALUE_TYPE": "MOBILE"
                    }
                ]
            }
        }

        create_contact = call_method(venrot_instance, "crm.contact.add", contact_data)
        if "result" in create_contact:
            user_id = create_contact.get("result")
    if user_id:
        deal_data = {
            "fields": {
                "TITLE": f"Установка приложения: {app_name}",
                "CONTACT_IDS": [user_id],
                "OPENED": "N",
            }
        }
        call_method(venrot_instance, "crm.deal.add", deal_data)
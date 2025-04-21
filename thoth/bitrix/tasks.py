import redis
from celery import shared_task
from celery.utils.log import get_task_logger
from django.core.exceptions import ObjectDoesNotExist

from .crest import call_method
from .models import AppInstance


logger = get_task_logger(__name__)
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


@shared_task(bind=True, max_retries=5, default_retry_delay=5)
def call_api(self, application_token, method, payload):
    try:
        appinstance = AppInstance.objects.get(application_token=application_token)
        call_method(appinstance, method, payload)
    except (ObjectDoesNotExist, Exception) as exc:
        raise self.retry(exc=exc)


@shared_task
def get_app_info():
    app_instances = AppInstance.objects.all()
    for app_instance in app_instances:
        if app_instance.attempts < 3:
            call_method(app_instance, "app.info", {})


@shared_task(bind=True, max_retries=5, default_retry_delay=5)
def send_messages(self, app_instance_id, user_phone, text, connector,
                  line, sms=False, pushName=None,
                  message_id=None, attachments=None, profilepic_url=None, 
                  chat_id=None, chat_url=None, user_id=None):
    # если отправлено через смс, то открываем линию от имени клиента
    init_mesasge = "System: initiation message."
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
                        "text": init_mesasge if sms else text,
                        "id": message_id,
                        "files": attachments
                    }
                }
            ],
        }

        resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg)

        if resp and "result" in resp:
            result = resp.get("result", {})
            results = result.get("DATA", {}).get("RESULT", [])
            for result in results:
                chat_session = result.get("session", {})
                if chat_session:
                    domain = app_instance.portal.domain
                    chat_id = chat_session.get("CHAT_ID")
                    identity = user_id or user_phone
                    redis_client.set(f"bitrix_chat:{domain}:{line}:{identity}", chat_id)
                    # если отправлено через смс досылаем текст сообщения клиенту
                    if sms:
                        message_add.delay(app_instance_id, line, user_phone, text, connector)
        else:
            logger.error(f"No result found in response: {resp}")
            raise Exception("Failed to send messages due to empty response or missing result.")

        return resp

    except ObjectDoesNotExist:
        logger.error(f"AppInstance with id {app_instance_id} does not exist.")
    except Exception as e:
        logger.error(f"Error sending messages: {e}")
        # Retry the task
        raise self.retry(exc=e)



@shared_task(bind=True, max_retries=5, default_retry_delay=5)
def message_add(self, app_instance_id, line_id, user_phone, text, connector):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
    except AppInstance.DoesNotExist:
        logger.error(f"AppInstance {app_instance_id} does not exist")
        return {'error': f'AppInstance {app_instance_id} does not exist'}
    
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
        attempts = 0
        message_sent = False

        while not message_sent and attempts < max_send_attempts:
            try:
                resp = call_method(app_instance, "im.message.add", payload)
                
                if "result" in resp:
                    message_id = resp.get("result")
                    redis_client.setex(f'bitrix:{domain}:{message_id}', 600, message_id)
                    message_sent = True

                    payload = {
                        "CONNECTOR": connector,
                        "LINE": line_id,
                        "MESSAGES": [
                            {
                                "im": {
                                    "chat_id": chat_id,
                                    "message_id": message_id
                                }
                            }
                        ]
                    }

                    call_method(app_instance, "imconnector.send.status.delivery", payload)
                    return resp
                elif "error" in resp:
                    error = resp.get("error")
                    
                    if error == "CANCELED":
                        call_method(app_instance, "imopenlines.session.join", {"CHAT_ID": chat_id})
                    else:
                        logger.error(f"Error sending message: {resp}")
                        break

            except Exception as e:
                logger.error(f"Exception occurred while sending message: {e}")
                raise self.retry(exc=e)

            attempts += 1
        
        if not message_sent:
            logger.error(f"Failed to send message after {attempts} attempts")
            return {'error': 'Failed to send message after multiple attempts'}
    
    else:
        send_messages.delay(app_instance_id, user_phone, text, connector, line_id, True)


@shared_task
def create_deal(app_instance_id, thoth_bitrix, app_name):
    app_instance = AppInstance.objects.get(id=app_instance_id)
    resp = call_method(app_instance, "user.current", {})
    if "result" in resp:
        thoth_instance = AppInstance.objects.get(id=thoth_bitrix)
        resp = resp.get("result", {})
        contact_data = {
            "fields": {
                "NAME": resp.get("NAME"),
                "LAST_NAME": resp.get("LAST_NAME"),
                "EMAIL": [
                    {
                        "VALUE": resp.get("EMAIL"),
                        "VALUE_TYPE": "WORK"
                    }
                ],
                "PHONE": [
                    {
                        "VALUE": resp.get("WORK_PHONE"),
                        "VALUE_TYPE": "WORK"
                    },
                    {
                        "VALUE": resp.get("PERSONAL_MOBILE"),
                        "VALUE_TYPE": "MOBILE"
                    }
                ]
            }
        }

        create_contact = call_method(thoth_instance, "crm.contact.add", contact_data)
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
                call_method(thoth_instance, "crm.deal.add", deal_data)
import logging
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
import requests
import redis
from celery import shared_task

import thoth.bitrix.crest as bitrix
import thoth.bitrix.tasks as bitrix_tasks

from .models import OlxUser
from .utils import deactivate_task

logger = logging.getLogger("django")
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

@shared_task
def refresh_token(olx_user_id):
    user = OlxUser.objects.get(olx_id=olx_user_id)
    olx_app = user.olxapp
    api_url = f"https://www.{olx_app.client_domain}/api/open/oauth/token"

    payload = {
        "grant_type": "refresh_token",
        "client_id": olx_app.client_id,
        "client_secret": olx_app.client_secret,
        "refresh_token": user.refresh_token,
    }

    get_token = requests.post(api_url, json=payload)
    if user.status != get_token.status_code:
        user.status = get_token.status_code

    if get_token.status_code == 200:
        user.attempts = 0
        token_data = get_token.json()
        user.access_token = token_data.get("access_token")
        user.refresh_token = token_data.get("refresh_token")
        user.save()
    else:
        user.attempts += 1
        user.save()
        deactivate_task(user.olx_id)
    return get_token


@shared_task
def refresh_tokens():
    accounts = OlxUser.objects.all()
    for account in accounts:
        if account.attempts > settings.OLX_CHECK_ATTEMTS:
            continue
        if account.date_end and timezone.now() > account.date_end:
            refresh_token.delay(account.olx_id)


@shared_task
def send_message(chat_id, text, files=None):
    threadid, olx_user_id, _ = chat_id.split("-")
    user = OlxUser.objects.get(olx_id=olx_user_id)
    api_url = f"https://www.{user.olxapp.client_domain}/api/partner/threads/{threadid}/messages"

    headers = {
        "Authorization": f"Bearer {user.access_token}",
        "Version": "2.0",
    }

    payload = {"text": text}
    if files:
        payload.update({
            "text": "files",
            "attachments": [{"url": file["link"]} for file in files]
        })

    response = requests.post(api_url, headers=headers, json=payload)

    if response.status_code == 401 and refresh_token(olx_user_id):
        headers["Authorization"] = f"Bearer {user.access_token}"
        response = requests.post(api_url, headers=headers, json=payload)


    if response.status_code == 200:
        msg_data = response.json().get("data")
        message_id = msg_data.get("id")
        redis_client.set(f'olx:{threadid}', message_id)

    return response.json()


@shared_task
def get_threads(olx_user_id):
    try:
        user = OlxUser.objects.get(olx_id=olx_user_id)
        connector_code = user.line.connector.code
        bitrix_user = user.line.portal.user_id

        if user.date_end and timezone.now() > user.date_end:
            deactivate_task(olx_user_id)
            payload = {
                'USER_ID': bitrix_user,
                'MESSAGE': 'Проверка сообщений на OLX остановлена в связи с окончанием действия тарифа. Тарифы и сопособы оплаты на сайте https://gulin.kz/'
            }
            bitrix.call_method(user.line.app_instance, 'im.notify.system.add', payload)
            return "Subscription expired, task terminated."

        olx_app = user.olxapp
        BASE_URL = f"https://www.{olx_app.client_domain}"
        api_url = f"{BASE_URL}/api/partner/threads/"
        headers = {
            "Authorization": f"Bearer {user.access_token}",
            "Version": "2.0",
        }

        response = requests.get(api_url, headers=headers)
        if user.status != response.status_code:
            user.status = response.status_code
            user.save()
            
        if response.status_code == 200:
            threads = response.json().get("data", [])
            # Обрабатываем каждый thread
            for thread in threads:
                unread_count = thread.get("unread_count", 0)
                if unread_count == 0:
                    continue
                thread_id = thread.get("id")
                advert_id = thread.get("advert_id")
                advert_url = f"{BASE_URL}/d/{advert_id}/"
                interlocutor_id = thread.get("interlocutor_id")
                chat_id = f"{thread_id}-{olx_user_id}-{interlocutor_id}"
                # получить имя пользователя
                user_url = f"{BASE_URL}/api/partner/users/{interlocutor_id}"
                user_info = requests.get(user_url, headers=headers)
                if user_info.status_code == 200:
                    user_data = user_info.json().get("data", {})
                    user_name = user_data.get("name")
                messages_url = f"{BASE_URL}/api/partner/threads/{thread_id}/messages"
                messages = requests.get(messages_url, headers=headers)
                if messages.status_code == 200:
                    messages = messages.json().get("data", [])
                    # Если тред уже есть в базе
                    if redis_client.exists(f"olx:{thread_id}"):
                        last_message = redis_client.get(f"olx:{thread_id}").decode('utf-8')
                        for message in messages:
                            message_id = message.get("id")
                            message_type = message.get("type")
                            text = message.get("text")
                            attachments = message.get("attachments", [])

                            if int(message_id) > int(last_message):
                                redis_client.set(f'olx:{thread_id}', message_id)
                                if message_type == "received":
                                    bitrix_tasks.send_messages.delay(user.line.app_instance.id, None, text, connector_code,
                                                                        user.line.line_id, False, user_name, message_id,
                                                                        attachments, None, chat_id, advert_url, interlocutor_id)
                                elif message_type == "sent":
                                    bitrix_tasks.message_add.delay(user.line.app_instance.id, user.line.line_id, 
                                                                    interlocutor_id, text, connector_code)
                    
                    # если треда нет в базе, то берем послденее полученное сообщение
                    else:
                        received_messages = [message for message in messages if message['type'] == 'received']
                        message = received_messages[-1] if received_messages else None
                        if message:
                            message_id = message.get("id")
                            text = message.get("text")
                            attachments = message.get("attachments", [])
                            redis_client.set(f'olx:{thread_id}', message_id)
                            bitrix_tasks.send_messages.delay(user.line.app_instance.id, None, text, connector_code,
                                                                user.line.line_id, False, user_name, message_id,
                                                                attachments, None, chat_id, advert_url, interlocutor_id)

                                
                commands_url = f"{BASE_URL}/api/partner/threads/{thread_id}/commands"
                resp = requests.post(commands_url, headers=headers, json={"command": "mark-as-read"})

        
        elif response.status_code == 401:
            resp = refresh_token(olx_user_id)
            if resp.status_code != 200:
                if user.line:
                    domain = user.line.app_instance.app.site
                    message = f'Проверка сообщений на OLX ({olx_user_id}) остановлена из-за проблемы. Переподключите аккаунт OLX на сайте https://{domain}/olx/accounts/. Поллный текст ошибки {resp.json()}'
                    payload = {
                        'USER_ID': bitrix_user,
                        'MESSAGE': message
                    }

                    bitrix.call_method(user.line.app_instance, 'im.notify.system.add', payload)

                    send_mail(
                        subject="OLX отключен из-за проблемы",
                        message=message,
                        from_email=settings.EMAIL_HOST_USER,
                        recipient_list=[user.owner],
                        fail_silently=False,
                    )

        else:
            logger.error(f"Failed to retrieve threads {user.olx_id}. Response: {response.json()}")

    except OlxUser.DoesNotExist:
        logger.debug(f"User with ID {olx_user_id} does not exist.")
    except Exception as e:
        logger.debug(
            f"An error occurred while processing OLX threads for user {olx_user_id}: {e!s}",
        )

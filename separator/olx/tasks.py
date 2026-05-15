import logging
from django.utils import timezone
from django.utils.translation import gettext as _
from django.conf import settings
import requests
import redis
from celery import shared_task

import separator.bitrix.tasks as bitrix_tasks

from .models import (
    OlxAdvert,
    OlxCategory,
    OlxCategoryAttribute,
    OlxCity,
    OlxDistrict,
    OlxRegion,
    OlxThread,
    OlxUser,
)
from .utils import deactivate_task

logger = logging.getLogger("django")
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def _partner_request(user, method, path, **kwargs):
    base_url = f"https://www.{user.olxapp.client_domain}/api/partner"
    headers = {
        "Authorization": f"Bearer {user.access_token}",
        "Version": "2.0",
    }
    response = requests.request(method, f"{base_url}{path}", headers=headers, **kwargs)
    if response.status_code == 401:
        refresh_token(user.olx_id)
        user.refresh_from_db(fields=["access_token"])
        headers["Authorization"] = f"Bearer {user.access_token}"
        response = requests.request(method, f"{base_url}{path}", headers=headers, **kwargs)
    return response


def _items(response):
    payload = response.json()
    if isinstance(payload, dict):
        return payload.get("data", [])
    return payload

@shared_task(queue='olx')
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
        raise Exception(
            f"OLX refresh token failed for user {olx_user_id}: "
            f"{get_token.status_code} {get_token.text}"
        )
    return get_token.status_code


@shared_task(queue='olx')
def refresh_tokens():
    accounts = OlxUser.objects.all()
    for account in accounts:
        if account.attempts > settings.OLX_CHECK_ATTEMTS:
            continue
        refresh_token.delay(account.olx_id)


@shared_task(queue='olx')
def process_olx_pushups():
    now = timezone.now()
    adverts = OlxAdvert.objects.select_related("olx_user__olxapp").filter(
        pushup_enabled=True,
        pushup_interval_days__gt=0,
        next_pushup_at__lte=now,
    )
    for advert in adverts:
        user = advert.olx_user
        if user.date_end and now > user.date_end:
            continue

        response = _partner_request(
            user,
            "POST",
            f"/adverts/{advert.advert_id}/paid-features",
            json={
                "code": "pushup",
                "payment_method": advert.pushup_payment_method or "account",
            },
        )
        if response.status_code == 204:
            advert.last_pushup_at = now
            advert.next_pushup_at = advert.calculate_next_pushup_at(now)
            advert.last_pushup_error = ""
            advert.save(update_fields=["last_pushup_at", "next_pushup_at", "last_pushup_error"])
        else:
            advert.last_pushup_error = f"{response.status_code} {response.text}"[:2000]
            advert.next_pushup_at = advert.calculate_next_pushup_at(now)
            advert.save(update_fields=["last_pushup_error", "next_pushup_at"])


@shared_task(queue='olx')
def sync_olx_dictionaries(olx_user_id):
    user = OlxUser.objects.select_related("olxapp").get(olx_id=olx_user_id)
    client_domain = user.olxapp.client_domain

    regions_response = _partner_request(user, "GET", "/regions")
    regions_by_olx_id = {}
    if regions_response.status_code == 200:
        for item in _items(regions_response):
            region, _ = OlxRegion.objects.update_or_create(
                client_domain=client_domain,
                olx_id=item["id"],
                defaults={"name": item.get("name") or ""},
            )
            regions_by_olx_id[region.olx_id] = region

    offset = 0
    limit = 1000
    while True:
        cities_response = _partner_request(user, "GET", "/cities", params={"offset": offset, "limit": limit})
        if cities_response.status_code != 200:
            raise Exception(f"OLX cities sync failed: {cities_response.status_code} {cities_response.text}")
        cities = _items(cities_response)
        for item in cities:
            OlxCity.objects.update_or_create(
                client_domain=client_domain,
                olx_id=item["id"],
                defaults={
                    "region": regions_by_olx_id.get(item.get("region_id")),
                    "name": item.get("name") or "",
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                },
            )
        if len(cities) < limit:
            break
        offset += limit

    cities_by_olx_id = {
        city.olx_id: city
        for city in OlxCity.objects.filter(client_domain=client_domain)
    }
    districts_response = _partner_request(user, "GET", "/districts")
    if districts_response.status_code == 200:
        for item in _items(districts_response):
            city = cities_by_olx_id.get(item.get("city_id"))
            if not city:
                continue
            OlxDistrict.objects.update_or_create(
                client_domain=client_domain,
                olx_id=item["id"],
                defaults={
                    "city": city,
                    "name": item.get("name") or "",
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                },
            )

    synced_category_ids = set()

    def sync_categories(parent=None):
        params = {}
        if parent:
            params["parent_id"] = parent.olx_id
        response = _partner_request(user, "GET", "/categories", params=params)
        if response.status_code != 200:
            raise Exception(f"OLX categories sync failed: {response.status_code} {response.text}")
        for item in _items(response):
            category, _ = OlxCategory.objects.update_or_create(
                client_domain=client_domain,
                olx_id=item["id"],
                defaults={
                    "parent": parent,
                    "name": item.get("name") or "",
                    "photos_limit": item.get("photos_limit") or 0,
                    "is_leaf": bool(item.get("is_leaf")),
                },
            )
            if category.id in synced_category_ids:
                continue
            synced_category_ids.add(category.id)
            if category.is_leaf:
                sync_category_attributes(category)
            else:
                sync_categories(category)

    def sync_category_attributes(category):
        response = _partner_request(user, "GET", f"/categories/{category.olx_id}/attributes")
        if response.status_code != 200:
            return
        for item in _items(response):
            OlxCategoryAttribute.objects.update_or_create(
                category=category,
                code=item["code"],
                defaults={
                    "label": item.get("label") or item["code"],
                    "unit": item.get("unit"),
                    "validation": item.get("validation") or {},
                    "values": item.get("values") or [],
                },
            )

    sync_categories()
    return {
        "regions": OlxRegion.objects.filter(client_domain=client_domain).count(),
        "cities": OlxCity.objects.filter(client_domain=client_domain).count(),
        "districts": OlxDistrict.objects.filter(client_domain=client_domain).count(),
        "categories": OlxCategory.objects.filter(client_domain=client_domain).count(),
    }


@shared_task(queue='olx')
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

    if response.status_code == 401:
        refresh_token(olx_user_id)
        user.refresh_from_db(fields=["access_token"])
        headers["Authorization"] = f"Bearer {user.access_token}"
        response = requests.post(api_url, headers=headers, json=payload)

    if response.status_code == 200:
        msg_data = response.json().get("data")
        message_id = int(msg_data.get("id") or 0)
        thread_id = int(msg_data.get("thread_id") or threadid)
        olx_thread, _ = OlxThread.objects.get_or_create(
            olx_user=user,
            thread_id=thread_id,
            defaults={"last_message_id": 0, "total_count": 0},
        )
        if message_id > olx_thread.last_message_id:
            olx_thread.last_message_id = message_id
            olx_thread.save(update_fields=["last_message_id"])
        redis_client.set(f'olx:{threadid}', message_id)
    else:
        raise Exception(f"OLX send message failed: {response.status_code} {response.text}")

    return response.json()


@shared_task(queue='olx')
def get_threads(olx_user_id):
    try:
        user = OlxUser.objects.get(olx_id=olx_user_id)
        line = None
        if user.line:
            line = user.line
            connector_code = line.connector.code
        if not line:
            deactivate_task(olx_user_id)
            logger.info(f"OLX task deactivated for user {olx_user_id}: line is not connected.")
            return

        if user.date_end and timezone.now() > user.date_end:
            deactivate_task(olx_user_id)
            logger.info(f"OLX task deactivated for user {olx_user_id}: tariff expired.")
            return

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
                total_count = int(thread.get("total_count") or 0)
                thread_id = thread.get("id")
                advert_id = thread.get("advert_id")
                advert_url = f"{BASE_URL}/d/{advert_id}/"
                interlocutor_id = thread.get("interlocutor_id")
                chat_id = f"{thread_id}-{olx_user_id}-{interlocutor_id}"
                olx_thread, created = OlxThread.objects.get_or_create(
                    olx_user=user,
                    thread_id=thread_id,
                    defaults={"last_message_id": 0, "total_count": 0},
                )
                commands_url = f"{BASE_URL}/api/partner/threads/{thread_id}/commands"
                if not created and olx_thread.total_count == total_count:
                    if unread_count:
                        requests.post(commands_url, headers=headers, json={"command": "mark-as-read"})
                    continue

                # получить имя пользователя
                user_name = None
                user_url = f"{BASE_URL}/api/partner/users/{interlocutor_id}"
                user_info = requests.get(user_url, headers=headers)
                if user_info.status_code == 200:
                    user_data = user_info.json().get("data", {})
                    user_name = user_data.get("name")
                messages_url = f"{BASE_URL}/api/partner/threads/{thread_id}/messages"
                messages = requests.get(messages_url, headers=headers)
                if messages.status_code != 200:
                    continue

                messages = sorted(messages.json().get("data", []), key=lambda item: int(item.get("id") or 0))
                if not messages:
                    olx_thread.total_count = total_count
                    olx_thread.save(update_fields=["total_count"])
                    continue

                last_message_id = olx_thread.last_message_id
                if created and unread_count == 0:
                    olx_thread.last_message_id = max(int(message.get("id") or 0) for message in messages)
                    olx_thread.total_count = total_count
                    olx_thread.save(update_fields=["last_message_id", "total_count"])
                    continue

                for message in messages:
                    message_id = int(message.get("id") or 0)
                    if message_id <= last_message_id:
                        continue
                    if created and not (message.get("type") == "received" and not message.get("is_read")):
                        continue

                    message_type = message.get("type")
                    text = message.get("text")
                    attachments = message.get("attachments", [])
                    if message_type == "received":
                        bitrix_tasks.send_messages.delay(line.app_instance.id, None, text, connector_code,
                                                            line.line_id, False, user_name, message_id,
                                                            attachments, None, chat_id, advert_url, interlocutor_id)
                    elif message_type == "sent":
                        bitrix_tasks.message_add.delay(line.app_instance.id, line.line_id,
                                                        interlocutor_id, text, connector_code,
                                                        attach=attachments)

                olx_thread.last_message_id = max(int(message.get("id") or 0) for message in messages)
                olx_thread.total_count = total_count
                olx_thread.save(update_fields=["last_message_id", "total_count"])
                                
                resp = requests.post(commands_url, headers=headers, json={"command": "mark-as-read"})
        
        elif response.status_code == 401:
            refresh_token(olx_user_id)

        else:
            raise Exception(f"Failed to retrieve threads {user.olx_id}. Response status: {response.status_code}")

    except OlxUser.DoesNotExist:
        logger.debug(f"User with ID {olx_user_id} does not exist.")
    except Exception as e:
        raise Exception(f"OLX threads for user {olx_user_id}: {e}")        

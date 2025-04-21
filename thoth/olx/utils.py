import logging
import requests
import redis
from django_celery_beat.models import PeriodicTask

from .models import OlxUser

logger = logging.getLogger("django")
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

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

    if get_token.status_code == 200:
        token_data = get_token.json()
        logger.info(f"NEW TOKEN {token_data}")

        # Сохраняем новые токены в базу данных
        user.access_token = token_data.get("access_token")
        user.refresh_token = token_data.get("refresh_token")
        user.save()
        logger.info(f"Tokens updated successfully for user {user.olx_id}")
        
    else:
        deactivate_task(user.olx_id)
        logger.debug(
            f"Failed to refresh token for user {user.olx_id}. Status code: {get_token.status_code}, Response: {get_token.json()}",
        )
        return get_token


def deactivate_task(olx_id):
    # Генерируем имя задачи
    task_name = f"Pull threads {olx_id}"

    # Пытаемся найти задачу по имени
    try:
        existing_task = PeriodicTask.objects.get(name=task_name)
        existing_task.enabled = False  # Деактивируем задачу
        existing_task.save()
        logger.info(f"Task '{task_name}' has been deactivated.")
    except PeriodicTask.DoesNotExist:
        logger.warning(f"Task '{task_name}' does not exist and cannot be deactivated.")
import logging
import redis
from django_celery_beat.models import PeriodicTask
from django.conf import settings


logger = logging.getLogger("django")
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def deactivate_task(olx_id):
    task_name = f"Pull threads {olx_id}"

    try:
        existing_task = PeriodicTask.objects.get(name=task_name)
        existing_task.enabled = False  # Деактивируем задачу
        existing_task.save()
    except PeriodicTask.DoesNotExist:
        logger.warning(f"Task '{task_name}' does not exist and cannot be deactivated.")
import logging
import redis
from django_celery_beat.models import PeriodicTask


logger = logging.getLogger("django")
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


def deactivate_task(olx_id):
    task_name = f"Pull threads {olx_id}"

    try:
        existing_task = PeriodicTask.objects.get(name=task_name)
        existing_task.enabled = False  # Деактивируем задачу
        existing_task.save()
    except PeriodicTask.DoesNotExist:
        logger.warning(f"Task '{task_name}' does not exist and cannot be deactivated.")
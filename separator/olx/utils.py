import logging
import redis
from django_celery_beat.models import PeriodicTask
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger("django")
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def activate_task(olx_user):
    if not olx_user.line:
        logger.info(f"OLX task for user {olx_user.olx_id} was not activated: line is not connected.")
        return

    olx_user.add_shedule_task()
    task_name = f"Pull threads {olx_user.olx_id}"

    try:
        existing_task = PeriodicTask.objects.get(name=task_name)
        if not existing_task.enabled:
            existing_task.enabled = True
            existing_task.last_run_at = timezone.now()
            existing_task.save(update_fields=["enabled", "last_run_at"])
    except PeriodicTask.DoesNotExist:
        logger.warning(f"Task '{task_name}' does not exist and cannot be activated.")


def deactivate_task(olx_id):
    task_name = f"Pull threads {olx_id}"

    try:
        existing_task = PeriodicTask.objects.get(name=task_name)
        existing_task.enabled = False
        existing_task.save(update_fields=["enabled"])
    except PeriodicTask.DoesNotExist:
        logger.warning(f"Task '{task_name}' does not exist and cannot be deactivated.")

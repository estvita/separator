from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from separator.waba.ctwa_events import CTWA_CONVERSION_EVENTS

from . import tasks as bitrix_tasks
from .models import AppInstance


CTWA_FIELD_CONFIGS = [
    {
        "FIELD_NAME": "SEPARATOR_CTWA_ID",
        "EDIT_FORM_LABEL": "CTWA ID",
        "LIST_COLUMN_LABEL": "CTWA ID",
        "USER_TYPE_ID": "string",
        "MULTIPLE": "N",
    },
    {
        "FIELD_NAME": "SEPARATOR_SOURCE_ID",
        "EDIT_FORM_LABEL": "Source ID",
        "LIST_COLUMN_LABEL": "Source ID",
        "USER_TYPE_ID": "string",
        "MULTIPLE": "N",
    },
]


CTWA_ROBOT_DATA = {
    "CODE": "separator_ctwa_tracker",
    "AUTH_USER_ID": 1,
    "NAME": {
        "ru": "Отправка CTWA",
        "en": "CTWA Tracker",
    },
    "PROPERTIES": {
        "ctwa_id": {
            "Name": {
                "ru": "CTWA ID",
                "en": "CTWA ID",
            },
            "Type": "string",
            "Required": "Y",
            "Multiple": "N",
            "Default": "{=Document:UF_CRM_SEPARATOR_CTWA_ID}",
        },
        "amount": {
            "Name": {
                "ru": "Сумма",
                "en": "Amount",
            },
            "Type": "double",
            "Required": "N",
            "Multiple": "N",
            "Default": "{=Document:OPPORTUNITY}",
        },
        "currency": {
            "Name": {
                "ru": "Валюта",
                "en": "Currency",
            },
            "Type": "string",
            "Required": "N",
            "Multiple": "N",
            "Default": "{=Document:CURRENCY_ID}",
        },
        "event_name": {
            "Name": {
                "ru": "Событие",
                "en": "Event",
            },
            "Type": "select",
            "Required": "N",
            "Multiple": "N",
            "Default": "Purchase",
            "Options": {event: event for event in CTWA_CONVERSION_EVENTS},
        },
    },
    "USE_PLACEMENT": "N",
}


def _queue_ctwa_setup(app_instance_id):
    for field_config in CTWA_FIELD_CONFIGS:
        payload = {"fields": field_config}
        bitrix_tasks.call_api.delay(app_instance_id, "crm.lead.userfield.add", payload)
        bitrix_tasks.call_api.delay(app_instance_id, "crm.deal.userfield.add", payload)

    bitrix_tasks.register_bizproc_robot.delay(app_instance_id, CTWA_ROBOT_DATA.copy())


@receiver(pre_save, sender=AppInstance)
def track_ctwa_change(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_ctwa = False
        return

    instance._previous_ctwa = (
        sender.objects.filter(pk=instance.pk).values_list("ctwa", flat=True).first() or False
    )


@receiver(post_save, sender=AppInstance)
def create_ctwa_fields_on_save(sender, instance, created, **kwargs):
    previous_ctwa = getattr(instance, "_previous_ctwa", False)
    ctwa_enabled = bool(instance.ctwa)

    if created and ctwa_enabled:
        transaction.on_commit(lambda: _queue_ctwa_setup(instance.id))
        return

    if not created and ctwa_enabled and not previous_ctwa:
        transaction.on_commit(lambda: _queue_ctwa_setup(instance.id))
        return

    if not created and previous_ctwa and not ctwa_enabled:
        transaction.on_commit(lambda: bitrix_tasks.delete_ctwa_fields.delay(instance.id))

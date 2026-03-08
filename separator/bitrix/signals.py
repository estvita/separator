from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from .models import AppInstance
from . import tasks as bitrix_tasks

@receiver(pre_save, sender=AppInstance)
def track_ctwa_change(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._old_ctwa = old_instance.ctwa
        except sender.DoesNotExist:
            instance._old_ctwa = False
    else:
        instance._old_ctwa = False

@receiver(post_save, sender=AppInstance)
def create_ctwa_fields_on_save(sender, instance, created, **kwargs):
    old_ctwa = getattr(instance, '_old_ctwa', False)
    
    if instance.ctwa and (created or not old_ctwa):
        try:        
            field_config = {
                "FIELD_NAME": "SEPARATOR_CTWA_ID",
                "EDIT_FORM_LABEL": "CTWA ID",
                "LIST_COLUMN_LABEL": "CTWA ID",
                "USER_TYPE_ID": "string",
                "MULTIPLE": "N",
            }

            payload = {
                "fields": field_config
            }
            # 1. Добавляем в Лиды
            bitrix_tasks.call_api.delay(instance.id, "crm.lead.userfield.add", payload)

            # 2. Добавляем в Сделки
            bitrix_tasks.call_api.delay(instance.id, "crm.deal.userfield.add", payload)

            robot_data = {
                "CODE": "separator_ctwa_tracker",
                "AUTH_USER_ID": 1,
                "NAME": {
                    "ru": "Отправка CTWA",
                    "en": "CTWA Tracker"
                },
                "PROPERTIES": {
                    "ctwa_id": {
                        "Name": {
                            "ru": "CTWA ID",
                            "en": "CTWA ID"
                        },
                        "Type": "string",
                        "Required": "Y",
                        "Multiple": "N",
                        "Default": "{=Document:UF_CRM_SEPARATOR_CTWA_ID}"
                    },
                    "amount": {
                        "Name": {
                            "ru": "Сумма",
                            "en": "Amount"
                        },
                        "Type": "double",
                        "Required": "N",
                        "Multiple": "N",
                        "Default": "{=Document:OPPORTUNITY}"
                    },
                    "currency": {
                        "Name": {
                            "ru": "Валюта",
                            "en": "Currency"
                        },
                        "Type": "string",
                        "Required": "N",
                        "Multiple": "N",
                        "Default": "{=Document:CURRENCY_ID}"
                    }
                },
                "USE_PLACEMENT": "N"
            }
            bitrix_tasks.register_bizproc_robot.delay(instance.id, robot_data)

        except Exception:
            raise
    
    elif not instance.ctwa and not created and old_ctwa:
        bitrix_tasks.delete_ctwa_fields.delay(instance.id)
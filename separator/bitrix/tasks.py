import re
import os
import redis
import logging
from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .crest import BitrixAccessDeniedError, call_method, refresh_token
from .models import AppInstance, Credential

from separator.waba.models import Phone
from separator.waweb.models import Session
from separator.olx.models import OlxUser
from separator.users.models import Message, User
from separator.bitbot.models import ChatBot
from separator.asterx.models import Server as AsterxServer

logger = logging.getLogger("django")

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def build_lead_title(site, code, fallback, **context):
    template = Message.objects.filter(site=site, code=code).first() if site else None
    text = template.message if template and template.message else fallback
    try:
        return text.format(**context)
    except Exception:
        return fallback.format(**context)


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def call_api(self, id, method, payload, b24_user=None):
    try:
        app_instance = AppInstance.objects.get(id=id)
        resp = call_method(app_instance, method, payload, b24_user_id=b24_user, timeout=10)
        return resp
    except BitrixAccessDeniedError:
        raise
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
        if need_refresh:
            refresh_token(credential)


@shared_task(queue='bitrix')
def get_app_info(instance_id=None):
    if instance_id is None:
        app_instance_ids = AppInstance.objects.filter(
            portal__isnull=False,
            portal__license_expired=False,
        ).values_list("id", flat=True)
        for app_instance_id in app_instance_ids:
            get_app_info.delay(app_instance_id)
        return

    app_instance = AppInstance.objects.filter(
        portal__isnull=False,
        portal__license_expired=False,
        id=instance_id,
    ).first()
    if not app_instance:
        return None

    try:
        resp = call_method(app_instance, "app.info", {})
        license_value = (resp.get("result") or {}).get("LICENSE")
        if license_value is not None and app_instance.portal.license != license_value:
            app_instance.portal.license = license_value
            app_instance.portal.save(update_fields=["license"])
        return resp
    except BitrixAccessDeniedError:
        if app_instance.owner_id:
            lead_title = build_lead_title(
                app_instance.owner.site if app_instance.owner_id else None,
                "bitrix_license_title",
                "License expired for portal {portal}",
                portal=app_instance.portal.domain,
            )
            try:
                prepare_lead.delay(app_instance.owner_id, lead_title)
            except Exception:
                pass
        raise


# Регистрация SMS-провайдера
@shared_task(queue='bitrix')
def messageservice_add(app_instance_id, entity_id, service):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
        if service == "waweb":
            entity = Session.objects.get(id=entity_id)
        elif service == "waba":
            entity = Phone.objects.get(id=entity_id)
        if hasattr(entity, 'sms_service'):
            phone = re.sub(r'\D', '', entity.phone)
            domain = app_instance.app.site.domain
            code = f"{domain}_{phone}"

            if entity.sms_service:
                try:
                    providers = call_method(app_instance, "messageservice.sender.list", admin=True)
                except Exception as e:
                    raise Exception(f"list providers fail: {e}")
                
                if "result" in providers and code in providers.get("result"):
                    raise Exception(f"{code} already exists")

                payload = {
                    "CODE": code,
                    "NAME": code,
                    "TYPE": "SMS",
                    "HANDLER": f"https://{domain}/api/bitrix/sms/?service={service}",
                }
                return call_method(app_instance, "messageservice.sender.add", payload, admin=True)
            else:
                return call_method(app_instance, "messageservice.sender.delete", {"CODE": code}, admin=True)

        else:
            raise Exception("Entity has no sms_service attribute!")
    except Exception as e:
        raise


@shared_task(queue='bitrix', bind=True)
def save_ctwa(self, instace_id, ctwa_id, chat_id, source_id=None):
    try:
        app_instance = AppInstance.objects.get(id=instace_id)
        dialog_data = call_method(app_instance, "imopenlines.dialog.get", {"CHAT_ID": chat_id})
        dialog_data = dialog_data.get("result", {})
        entity_data_2 = dialog_data.get("entity_data_2", "")
        
        # Парсинг строки вида "LEAD|0|COMPANY|0|CONTACT|9014|DEAL|10008"
        parts = str(entity_data_2).split("|")
        entity_dict = {}
        for i in range(0, len(parts) - 1, 2):
            entity_dict[parts[i]] = parts[i+1]
            
        lead_id = entity_dict.get("LEAD")
        deal_id = entity_dict.get("DEAL")

        fields = {}
        if ctwa_id:
            fields["UF_CRM_SEPARATOR_CTWA_ID"] = str(ctwa_id)
        if source_id is not None:
            fields["UF_CRM_SEPARATOR_SOURCE_ID"] = str(source_id)
        if not fields:
            return

        if lead_id and str(lead_id) != "0":
            call_api.delay(app_instance.id, "crm.lead.update", {
                "id": lead_id,
                "fields": fields
            })
            
        if deal_id and str(deal_id) != "0":
            call_api.delay(app_instance.id, "crm.deal.update", {
                "id": deal_id,
                "fields": fields
            })
        
    except Exception as e:
        logger.error(f"Error in save_ctwa: {e}")
        pass


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def send_messages(self, app_instance_id, user_phone, text, connector,
                  line, sms=False, pushName=None,
                  message_id=None, attachments=None, profilepic_url=None,
                  chat_id=None, chat_url=None, user_id=None, ctwa_id=None, source_id=None, manager_id=None):
    init_message = "Создание чата..."
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
                        "url": chat_url,
                    },
                    "message": {
                        "text": init_message if sms else text,
                        "id": message_id,
                        "files": attachments if not sms else [],
                        "user_id": manager_id,
                    }
                }
            ],
        }
        resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg, timeout=30)

        result = resp.get("result", {})
        results = result.get("DATA", {}).get("RESULT", [])
        retried_without_session = False
        if results and not any(result_item.get("session", {}) for result_item in results) and not retried_without_session:
            retried_without_session = True
            resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg, timeout=30)
            result = resp.get("result", {})
            results = result.get("DATA", {}).get("RESULT", [])
        for result_item in results:
            chat_session = result_item.get("session", {})
            if chat_session:
                member_id = app_instance.portal.member_id
                chat_id = chat_session.get("CHAT_ID")
                identity = user_id or user_phone
                try:
                    redis_client.set(f"bitrix_chat:{member_id}:{line}:{identity}", chat_id, ex=2592000)
                except Exception:
                    pass
                if sms:
                    message_add.delay(app_instance_id, line, user_phone, text, connector, attach=attachments)
                
                # https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging/#ads-that-click-to-whatsapp
                if app_instance.ctwa and chat_id and (ctwa_id or source_id is not None):
                    save_ctwa.delay(app_instance_id, ctwa_id, chat_id, source_id=source_id)
        return results

    except Exception as e:
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def message_add(self, app_instance_id, line_id, user_phone, text, connector, attach=None):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
    except AppInstance.DoesNotExist:
        logger.error(f"AppInstance {app_instance_id} does not exist")
        raise

    member_id = app_instance.portal.member_id
    chat_keys = [f'bitrix_chat:{member_id}:{line_id}:{user_phone}']
    
    chat_id = None
    try:
        for chat_key in chat_keys:
            if redis_client.exists(chat_key):
                chat_id = redis_client.get(chat_key).decode('utf-8')
                break
    except Exception:
        pass

    if chat_id:
        payload = {
            "DIALOG_ID": f"chat{chat_id}",
            "MESSAGE": text or " ",
            "SYSTEM": "Y",
            "ATTACH": attach
        }
        max_send_attempts = 3

        for attempt in range(max_send_attempts):
            try:
                # try:
                #     call_method(app_instance, "imopenlines.session.start", {"CHAT_ID": chat_id})
                # except Exception as e:
                #     logger.warning(f"Failed to start session for chat {chat_id}: {e}")
                resp = call_method(app_instance, "im.message.add", payload, timeout=10)
                # message_id = resp.get("result")
                # try:
                #     redis_client.setex(f'bitrix:{member_id}:{message_id}', 600, message_id)
                # except Exception:
                #     pass
                # payload_status = {
                #     "CONNECTOR": connector,
                #     "LINE": line_id,
                #     "MESSAGES": [{
                #         "im": {
                #             "chat_id": chat_id,
                #             "message_id": message_id
                #         }
                #     }]
                # }
                # call_api.delay(app_instance.id, "imconnector.send.status.delivery", payload_status)
                return resp
            except Exception as e:
                if attempt >= max_send_attempts - 1:
                    logger.error(f"Exception occurred while sending message: {e}")
                    raise
                else:
                    self.retry(exc=e)
    else:
        return send_messages(app_instance_id, user_phone, text, connector, line_id, True, attachments=attach)


@shared_task(queue='bitrix')
def prepare_lead(user_id, lead_title):
    user = User.objects.filter(id=user_id).first()
    if not user:
        raise Exception("user not found")
    site = user.site
    if not site:
        raise Exception(f"site for {user.email} not found")
    if not site.profile:
        raise Exception(f"site.profile for {site.domain} not found")
    owner = site.profile.owner
    vendor_instance = AppInstance.objects.filter(owner=owner, app__vendor=True).first()
    if not vendor_instance:
        raise Exception(f"vendor instance for vendor {owner.email} not found")
    payload = {
        "filter": {
            "0": {
                "logic": "OR",
                "0": {"phone": str(user.phone_number)},
                "1": {"email": user.email}
            }
        },
        "entityTypeId": 3, #contacts
    }
    client_data = call_method(vendor_instance, "crm.item.list", payload)
    contact_ids = []
    if "result" in client_data:
        client_data = client_data.get("result", {}).get("items", [])
        contact_ids = [contact.get("id") for contact in client_data if contact.get("id")]

    lead_data = {
        "fields": {
            "TITLE": lead_title,
        }
    }
    if contact_ids:
        lead_data["fields"]["CONTACT_IDS"] = contact_ids
    else:
        lead_data["fields"]["NAME"] = user.name
        lead_data["fields"]["EMAIL"] = [{
            "VALUE": user.email,
            "VALUE_TYPE": "WORK"
        }]
        lead_data["fields"]["PHONE"] = [{
            "VALUE": str(user.phone_number),
            "VALUE_TYPE": "MOBILE"
        }]

    return call_method(vendor_instance, "crm.lead.add", lead_data)


def auto_finish_chat(instance_id, data):
    try:
        document_id = data.get("document_id[2]")
        if not document_id or "_" not in document_id:
            raise ValueError(f"Missing or invalid document_id in bizproc data: {document_id}")
        entity_type, entity_id = document_id.split("_", 1)

        if not entity_type or not entity_id:
            raise ValueError(f"Missing entity information in bizproc data: {data}")
        entity_type = entity_type.upper()
        if entity_type not in ("DEAL", "LEAD"):
            raise ValueError(f"Unsupported entity type in bizproc data: {entity_type}")
        app_instance = AppInstance.objects.get(id=instance_id)
        payload = {
            "CRM_ENTITY_TYPE": entity_type,
            "CRM_ENTITY": entity_id
        }
        chat_data = call_method(app_instance, "imopenlines.crm.chat.getLastId", payload, admin=True)
        if "result" in chat_data:
            chat_id = chat_data.get("result")
            return call_method(app_instance, "imopenlines.operator.another.finish", {"CHAT_ID": chat_id}, admin=True)
        else:
            raise Exception(f"chat not found: {chat_data}")
    except Exception:
        raise

@shared_task(queue='default')
def delete_temp_file(file_path):
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Deleted temp file: {file_path}")
        except Exception as e:
            logger.error(f"Error deleting temp file {file_path}: {e}")


@shared_task(queue='bitrix')
def check_tariffs(*days):
    """
    Check tariff expiration for WABA, WaWeb, OLX, BitBot, and AsterX.
    Accepts multiple integer arguments.
    Example: check_tariffs(10, 5, 1)
    """
    days_list = [d for d in days if isinstance(d, int)]

    if not days_list:
        return

    today = timezone.now().date()
    max_days = max(days_list) if days_list else 1
    ttl = max_days * 24 * 60 * 60

    mapping = [
        (Phone, 'date_end', 'phone', 'waba'),
        (Session, 'date_end', 'phone', 'waweb'),
        (OlxUser, 'date_end', 'olx_id', 'olx'),
        (ChatBot, 'date_end', 'name', 'bitbot'),
        (AsterxServer, 'date_end', 'name', 'asterx'),
    ]

    for days in days_list:
        target_date = today + timedelta(days=days)

        for Model, date_field, id_field, service in mapping:
            filter_kwargs = {
                f"{date_field}__year": target_date.year,
                f"{date_field}__month": target_date.month,
                f"{date_field}__day": target_date.day,
            }

            for record in Model.objects.filter(**filter_kwargs):
                if not record.owner:
                    continue

                redis_key = f"leads:{service}:{record.id}"
                if redis_client.get(redis_key):
                    continue

                identifier = getattr(record, id_field, 'Unknown')
                expiration_str = record.date_end.strftime('%d.%m.%Y')
                title = build_lead_title(
                    record.owner.site if record.owner else None,
                    "service_subscription_title",
                    "Subscription for {service}: {identifier} expires on {expiration_date}",
                    service=service,
                    identifier=identifier,
                    expiration_date=expiration_str,
                )

                try:
                    resp = prepare_lead(record.owner.id, title)
                    if resp and isinstance(resp, dict) and 'result' in resp:
                        redis_client.setex(redis_key, ttl, resp['result'])
                except Exception as e:
                    print(e)


@shared_task(queue='bitrix')
def register_bizproc_robot(appinstance_id, payload=None):
    try:
        appinstance = AppInstance.objects.get(id=appinstance_id)
        url = appinstance.app.site
        if payload:
            payload["HANDLER"] = f"https://{url}/api/bitrix/bizproc/"
            call_api.delay(appinstance.id, "bizproc.robot.add", payload)
    except Exception:
        raise

@shared_task(queue='bitrix')
def delete_ctwa_fields(app_instance_id):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
        
        for entity_type in ["lead", "deal"]:
            list_method = f"crm.{entity_type}.userfield.list"
            for field_name in ["UF_CRM_SEPARATOR_CTWA_ID", "UF_CRM_SEPARATOR_SOURCE_ID"]:
                fields = call_method(app_instance, list_method, {"filter": {"FIELD_NAME": field_name}})

                field_id = None
                if fields and "result" in fields and len(fields["result"]) > 0:
                    field_id = fields["result"][0].get("ID")

                if field_id:
                    call_api.delay(app_instance_id, f"crm.{entity_type}.userfield.delete", {"id": field_id})
                
        call_api.delay(app_instance_id, "bizproc.robot.delete", {"CODE": "separator_ctwa_tracker"})
    except Exception as e:
        logger.error(f"Error deleting CTWA fields: {e}")

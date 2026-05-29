import re
import os
import redis
import logging
from copy import deepcopy
from urllib.parse import urljoin
from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .crest import BitrixAccessDeniedError, call_method, refresh_token
from .models import ApiCall, AppInstance, Credential, Feature, FeatureGrant

from separator.waba.models import Phone
from separator.waweb.models import Session
from separator.olx.models import OlxUser
from separator.users.models import Message, User
from separator.bitbot.models import ChatBot
from separator.asterx.models import Server as AsterxServer

logger = logging.getLogger("django")

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def _feature_owner(app_instance):
    if app_instance.owner_id:
        return app_instance.owner
    if app_instance.portal and app_instance.portal.owner_id:
        return app_instance.portal.owner
    return None


def _render_feature_value(value, context):
    if isinstance(value, dict):
        return {key: _render_feature_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_feature_value(item, context) for item in value]
    if isinstance(value, str):
        try:
            return value.format(**context)
        except Exception:
            return value
    return value


def _normalize_handler_url(value, base_url):
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(("http://", "https://")):
        return value
    return urljoin(base_url, value.lstrip("/"))


def _build_feature_payloads(feature, app_instance, placement_code=None):
    site_domain = str(app_instance.app.site).strip().strip("/") if app_instance.app and app_instance.app.site else ""
    app_base_url = f"https://{site_domain}/" if site_domain else ""
    context = {
        "app_base_url": app_base_url.rstrip("/"),
        "site_domain": site_domain,
        "app_instance_id": app_instance.id,
        "portal_domain": app_instance.portal.domain if app_instance.portal else "",
        "portal_protocol": app_instance.portal.protocol if app_instance.portal else "https",
        "placement": placement_code or "",
    }
    raw_payload = deepcopy(feature.payload or {})
    raw_payload = _render_feature_value(raw_payload, context)

    if isinstance(raw_payload, dict):
        payloads = [raw_payload]
    elif isinstance(raw_payload, list):
        payloads = raw_payload
    else:
        raise Exception(f"Feature {feature.id} payload must be a JSON object or list of objects")

    prepared_payloads = []
    for index, payload in enumerate(payloads, start=1):
        if not isinstance(payload, dict):
            raise Exception(f"Feature {feature.id} payload item {index} must be a JSON object")

        payload = dict(payload)
        if feature.code and (len(payloads) == 1 or "CODE" not in payload):
            payload["CODE"] = feature.code
        if placement_code and "PLACEMENT" not in payload:
            payload["PLACEMENT"] = placement_code
        if feature.method == "placement.bind" and feature.name and "TITLE" not in payload:
            payload["TITLE"] = feature.name

        for key in ("HANDLER", "PLACEMENT_HANDLER"):
            if key in payload:
                payload[key] = _normalize_handler_url(payload[key], app_base_url)

        prepared_payloads.append(payload)

    return prepared_payloads


def _feature_date_end(app_instance, code, existing_grant=None):
    if existing_grant and existing_grant.date_end is not None:
        return existing_grant.date_end
    owner = _feature_owner(app_instance)
    if not owner or not code:
        return None
    from separator.tariff.utils import get_trial
    return get_trial(owner, code)


def build_lead_title(site, code, fallback, **context):
    template = Message.objects.filter(site=site, code=code).first() if site else None
    text = template.message if template and template.message else fallback
    try:
        return text.format(**context)
    except Exception:
        return fallback.format(**context)


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def call_api(self, id, method, payload, b24_user=None):
    # Keep this wrapper aligned with the historical default behavior of call_method().
    # Changing its implicit admin-mode semantics breaks existing install/runtime flows
    # that call bitrix_tasks.call_api(...) without extra flags.
    try:
        app_instance = AppInstance.objects.get(id=id)
        resp = call_method(app_instance, method, payload, b24_user_id=b24_user, timeout=10)
        return resp
    except BitrixAccessDeniedError:
        raise
    except (ObjectDoesNotExist, Exception) as exc:
        raise self.retry(exc=exc)


@shared_task(queue="bitrix")
def dispatch_api_call(api_call_id):
    api_call = ApiCall.objects.select_related("app").get(id=api_call_id)
    payload = api_call.payload or {}

    if not api_call.app_id:
        raise Exception(f"ApiCall {api_call.id} has no app selected")
    if not api_call.method:
        raise Exception(f"ApiCall {api_call.id} has no method")
    if not isinstance(payload, dict):
        raise Exception(f"ApiCall {api_call.id} payload must be a JSON object")

    app_instance_ids = list(
        AppInstance.objects.filter(app_id=api_call.app_id).values_list("id", flat=True)
    )
    if not app_instance_ids:
        raise Exception(f"No AppInstances found for app {api_call.app_id}")

    queued = 0
    for app_instance_id in app_instance_ids:
        call_api.delay(app_instance_id, api_call.method, payload)
        queued += 1

    return {
        "api_call_id": api_call.id,
        "queued": queued,
    }


@shared_task(queue="bitrix")
def register_feature(app_instance_id, feature_id, placement_code=None, force=False):
    app_instance = AppInstance.objects.select_related("app", "portal", "owner").get(id=app_instance_id)
    feature = Feature.objects.prefetch_related("apps").get(id=feature_id)

    if not force and not feature.active:
        raise Exception(f"Feature {feature.id} is inactive")
    if app_instance.app_id and not feature.apps.filter(id=app_instance.app_id).exists():
        raise Exception(
            f"Feature {feature.id} is not linked to app {app_instance.app_id}"
        )

    payloads = _build_feature_payloads(feature, app_instance, placement_code=placement_code)
    responses = []
    for payload in payloads:
        responses.append(call_method(app_instance, feature.method, payload, timeout=30))

    code = str(feature.code or "").strip() or None
    if code:
        if not app_instance.portal_id:
            raise Exception(f"AppInstance {app_instance.id} has no portal for FeatureGrant")
        existing_grant = FeatureGrant.objects.filter(portal=app_instance.portal, feature=feature).first()
        FeatureGrant.objects.update_or_create(
            portal=app_instance.portal,
            feature=feature,
            defaults={
                "date_end": _feature_date_end(app_instance, code, existing_grant=existing_grant),
            },
        )

    return responses[0] if len(responses) == 1 else responses


@shared_task(queue="bitrix")
def apply_feature_now(feature_id):
    feature = Feature.objects.prefetch_related("apps").get(id=feature_id)

    app_ids = list(feature.apps.values_list("id", flat=True))
    if not app_ids:
        raise Exception(f"Feature {feature.id} has no linked apps")

    app_instances = AppInstance.objects.filter(app_id__in=app_ids).distinct()
    placement_codes = [p.strip() for p in (feature.placements or "").splitlines() if p.strip()]

    queued = 0
    for app_instance in app_instances:
        if placement_codes:
            for placement_code in placement_codes:
                register_feature.delay(app_instance.id, feature.id, placement_code=placement_code, force=True)
                queued += 1
            continue

        register_feature.delay(app_instance.id, feature.id, force=True)
        queued += 1

    return {
        "feature_id": feature.id,
        "queued": queued,
    }

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
                  line, pushName=None,
                  message_id=None, attachments=None, profilepic_url=None,
                  chat_id=None, chat_url=None, user_id=None, ctwa_id=None, source_id=None, manager_id=None):
    try:
        app_instance = AppInstance.objects.get(id=app_instance_id)
        # BSUIDs from WhatsApp usernames include dots (for example, US.xxx), phone numbers do not.
        if user_phone and not str(user_phone).startswith("+") and "." not in str(user_phone):
            user_phone = f"+{user_phone}"
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
                        "text": text,
                        "id": message_id,
                        "files": attachments,
                        "user_id": manager_id,
                    }
                }
            ],
        }
        resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg, timeout=30)

        result = resp.get("result", {})
        results = result.get("DATA", {}).get("RESULT", [])
        needs_ctwa_session = ctwa_id or source_id is not None
        if needs_ctwa_session and results and not any(result_item.get("session", {}) for result_item in results):
            resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg, timeout=30)
            result = resp.get("result", {})
            results = result.get("DATA", {}).get("RESULT", [])
        for result_item in results:
            chat_session = result_item.get("session", {})
            if chat_session:
                chat_id = chat_session.get("CHAT_ID")
                # https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging/#ads-that-click-to-whatsapp
                if app_instance.has_active_feature("separator_ctwa_tracker") and chat_id and (ctwa_id or source_id is not None):
                    save_ctwa.delay(app_instance_id, ctwa_id, chat_id, source_id=source_id)
        return results

    except Exception as e:
        raise self.retry(exc=e)


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
    client_filter = {"email": user.email}
    if user.phone_number:
        client_filter = {
            "logic": "OR",
            "0": {"phone": str(user.phone_number)},
            "1": {"email": user.email}
        }
    payload = {
        "filter": {
            "0": client_filter,
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
            raise Exception(f"Deleted temp file: {file_path}")
        except Exception as e:
            raise Exception(f"Error deleting temp file {file_path}: {e}")


@shared_task(queue='bitrix')
def check_tariffs(*days):
    """
    Check expiration for service subscriptions and feature grants.
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

        feature_grants = FeatureGrant.objects.filter(
            date_end__year=target_date.year,
            date_end__month=target_date.month,
            date_end__day=target_date.day,
            portal__owner__isnull=False,
        ).select_related("feature", "portal", "portal__owner")

        for grant in feature_grants:
            owner = grant.portal.owner
            redis_key = f"leads:feature:{grant.id}"
            if redis_client.get(redis_key):
                continue

            feature_name = grant.feature.name
            expiration_str = grant.date_end.strftime('%d.%m.%Y')
            title = build_lead_title(
                owner.site,
                "service_subscription_title",
                "Subscription for {service}: expires on {expiration_date}",
                service=feature_name,
                expiration_date=expiration_str,
            )

            try:
                resp = prepare_lead(owner.id, title)
                if resp and isinstance(resp, dict) and 'result' in resp:
                    redis_client.setex(redis_key, ttl, resp['result'])
            except Exception as e:
                print(e)

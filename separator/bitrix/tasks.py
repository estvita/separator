import re
import os
import logging
import requests
from copy import deepcopy
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .crest import BitrixAccessDeniedError, call_method, refresh_token
from .models import ApiCall, AppInstance, Credential, Feature, FeatureGrant
from .retry import RETRY_KWARGS, TRANSIENT_ERRORS

from separator.waba.models import Phone
from separator.waweb.models import Session
from separator.users.models import User
import separator.home.tasks as home_tasks

logger = logging.getLogger("django")

class BitrixSendMessagesReadTimeout(Exception):
    pass


def _feature_owner(app_instance):
    if app_instance.owner_id:
        return app_instance.owner
    if app_instance.portal and app_instance.portal.owner_id:
        return app_instance.portal.owner
    return None


def _build_feature_payloads(feature, placement_code=None):
    raw_payload = deepcopy(feature.payload or {})

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
        if placement_code:
            payload["PLACEMENT"] = placement_code

        prepared_payloads.append(payload)

    return prepared_payloads


def _feature_date_end(app_instance, code, existing_grant=None):
    if existing_grant and existing_grant.date_end is not None:
        return existing_grant.date_end
    owner = _feature_owner(app_instance)
    if not owner or not code:
        return None
    from separator.home.utils import get_trial
    return get_trial(owner, code)


@shared_task(queue='bitrix', **RETRY_KWARGS)
def call_api(id, method, payload, b24_user=None):
    # Keep this wrapper aligned with the historical default behavior of call_method().
    # Changing its implicit admin-mode semantics breaks existing install/runtime flows
    # that call bitrix_tasks.call_api(...) without extra flags.
    app_instance = AppInstance.objects.get(id=id)
    return call_method(app_instance, method, payload, b24_user_id=b24_user, timeout=10)


@shared_task(queue="bitrix", **RETRY_KWARGS)
def register_voximplant_sip(phone_id, app_instance_id):
    phone = Phone.objects.select_related("sip_extensions__server").get(id=phone_id)
    if phone.call_dest != "b24":
        return {"status": "skipped", "reason": "call destination changed"}

    extension = phone.sip_extensions
    if not extension or not extension.server:
        raise Exception("SIP extension is not configured")

    server = extension.server.domain
    if settings.VOIP_SERVER_OPENSIPS:
        server = f"{server}:{extension.server.sip_port};transport=tls"

    payload = {
        "TITLE": f"{phone.phone} WhatsApp",
        "SERVER": server,
        "LOGIN": extension.number,
        "PASSWORD": extension.password,
    }
    app_instance = AppInstance.objects.get(id=app_instance_id)
    response = call_method(app_instance, "voximplant.sip.add", payload, timeout=10)
    result = response.get("result", {})
    voximplant_id = result.get("ID")
    voximplant_reg_id = result.get("REG_ID")
    if not voximplant_id or not voximplant_reg_id:
        raise Exception("Bitrix did not return Voximplant registration IDs")

    updated = Phone.objects.filter(
        id=phone_id,
        call_dest="b24",
    ).update(
        voximplant_id=int(voximplant_id),
        voximplant_reg_id=int(voximplant_reg_id),
    )
    if not updated:
        raise

    return result


@shared_task(queue="bitrix", **RETRY_KWARGS)
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


@shared_task(queue="bitrix", **RETRY_KWARGS)
def register_feature(app_instance_id, feature_id, placement_code=None, force=False):
    app_instance = AppInstance.objects.select_related("app", "portal", "owner").get(id=app_instance_id)
    feature = Feature.objects.prefetch_related("apps").get(id=feature_id)

    if not force and not feature.active:
        raise Exception(f"Feature {feature.id} is inactive")
    if app_instance.app_id and not feature.apps.filter(id=app_instance.app_id).exists():
        raise Exception(
            f"Feature {feature.id} is not linked to app {app_instance.app_id}"
        )

    payloads = _build_feature_payloads(feature, placement_code=placement_code)
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


@shared_task(queue="bitrix", **RETRY_KWARGS)
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

@shared_task(queue='bitrix', **RETRY_KWARGS)
def upd_refresh_token(period):
    now = timezone.now()
    credentials = Credential.objects.all()
    for credential in credentials:
        need_refresh = (
            credential.refresh_date is None or
            credential.refresh_date < now - timedelta(days=period)
        )
        if need_refresh:
            upd_cred_token.delay(credential.id)


@shared_task(queue='bitrix', **RETRY_KWARGS)
def upd_cred_token(credential_id):
    credential = Credential.objects.get(id=credential_id)
    refresh_token(credential, raise_request_exception=True)


@shared_task(queue='bitrix', **RETRY_KWARGS)
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
            lead_title = home_tasks.build_lead_title(
                app_instance.owner.site if app_instance.owner_id else None,
                "bitrix_license_title",
                "License expired for portal {portal}",
                portal=app_instance.portal.domain,
            )
            try:
                home_tasks.prepare_lead.delay(app_instance.owner_id, lead_title)
            except Exception:
                pass
        raise


# Регистрация SMS-провайдера
@shared_task(queue='bitrix', **RETRY_KWARGS)
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
                except TRANSIENT_ERRORS:
                    raise
                except Exception as e:
                    raise Exception(f"list providers fail: {e}")
                
                if "result" in providers and code in providers.get("result"):
                    raise Exception(f"{code} already exists")

                payload = {
                    "CODE": code,
                    "NAME": code,
                    "TYPE": "SMS",
                    "HANDLER": app_instance.app.get_bitrix_sms_handler_url(service),
                }
                return call_method(app_instance, "messageservice.sender.add", payload, admin=True)
            else:
                return call_method(app_instance, "messageservice.sender.delete", {"CODE": code}, admin=True)

        else:
            raise Exception("Entity has no sms_service attribute!")
    except Exception as e:
        raise


@shared_task(queue='bitrix', **RETRY_KWARGS)
def save_ctwa(instace_id, ctwa_id, chat_id, source_id=None):
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
        
    except TRANSIENT_ERRORS:
        raise
    except Exception as e:
        logger.error(f"Error in save_ctwa: {e}")
        pass


@shared_task(bind=True, max_retries=5, default_retry_delay=5, queue='bitrix')
def send_messages(self, app_instance_id, user_phone, text, connector,
                  line, pushName=None,
                  message_id=None, attachments=None, profilepic_url=None,
                  chat_id=None, chat_url=None, user_id=None, ctwa_id=None, source_id=None, manager_id=None,
                  referral_body=None):
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
                        "name": pushName or " ",
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
        if referral_body:
            bitrix_msg["MESSAGES"].append({
                "user": {
                    "id": user_id or user_phone,
                },
                "chat": {
                    "id": chat_id or user_phone,
                },
                "message": {
                    "text": referral_body,
                    "user_id": 0,
                },
            })
        request_timeout = 300 if attachments else 30
        resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg, timeout=request_timeout)

        result = resp.get("result", {})
        results = result.get("DATA", {}).get("RESULT", [])
        needs_ctwa_session = ctwa_id or source_id is not None
        if needs_ctwa_session and results and not any(result_item.get("session", {}) for result_item in results):
            resp = call_method(app_instance, "imconnector.send.messages", bitrix_msg, timeout=request_timeout)
            result = resp.get("result", {})
            results = result.get("DATA", {}).get("RESULT", [])
        failed_results = [
            result_item for result_item in results
            if isinstance(result_item, dict) and result_item.get("SUCCESS") is False
        ]
        if failed_results:
            raise Exception(resp)
        ctwa_chat_ids = set()
        for result_item in results:
            chat_session = result_item.get("session", {})
            if chat_session:
                chat_id = chat_session.get("CHAT_ID")
                # https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging/#ads-that-click-to-whatsapp
                if (
                    app_instance.has_active_feature("separator_ctwa_tracker")
                    and chat_id
                    and chat_id not in ctwa_chat_ids
                    and (ctwa_id or source_id is not None)
                ):
                    save_ctwa.delay(app_instance_id, ctwa_id, chat_id, source_id=source_id)
                    ctwa_chat_ids.add(chat_id)
        return resp

    except BitrixAccessDeniedError:
        raise
    except requests.exceptions.ReadTimeout as e:
        if attachments:
            # Future improvement: add Redis idempotency by message_id and mark this state as "unknown".
            # Then retries for attachments can be enabled without risking duplicate Bitrix messages.
            raise BitrixSendMessagesReadTimeout(
                "Bitrix send_messages timed out with attachments; retry skipped to avoid duplicates"
            ) from e
        raise self.retry(exc=e)
    except Exception as e:
        raise self.retry(exc=e)


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
            return f"chat not found: {chat_data}"
    except Exception:
        raise

@shared_task(queue='default')
def delete_temp_file(file_path):
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            return f"Deleted temp file: {file_path}"
        except Exception as e:
            raise Exception(f"Error deleting temp file {file_path}: {e}")



import time
import redis
import random
import os
import subprocess
import tempfile
import requests
import logging
from celery import shared_task

from .models import App, PartnerApp, Waba, Phone, Template, Error, Ctwa, CtwaEvents
from .retry import RETRY_KWARGS, TRANSIENT_ERRORS
import separator.waba.utils as utils

from separator.users.models import User
import separator.bitrix.tasks as bitrix_tasks

from django.conf import settings
from django.db import models
from django.utils import timezone


redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)
logger = logging.getLogger("waba")


def _normalize_phone_number(phone_number):
    return '+' + ''.join(filter(str.isdigit, phone_number or ''))


def _onboard_waba_assets(
    waba,
    user=None,
    register=True,
    target_phone_number=None,
    target_phone_id=None,
    save_templates=True,
    create_lead=True,
):
    # Common post-token onboarding for Embedded Signup and Hosted Embedded Signup.
    if save_templates:
        utils.save_approved_templates.delay(waba.id)

    resp = utils.call_api(waba=waba, endpoint=f"{waba.waba_id}/phone_numbers")
    phone_numbers = resp.get('data', {})


    target_phone_number = _normalize_phone_number(target_phone_number) if target_phone_number else None
    target_phone_id = str(target_phone_id) if target_phone_id else None
    matched_target = False

    for phone_data in phone_numbers:
        phone_type = "cloud"
        phone_id = phone_data.get('id')
        pin = f"{random.randint(0, 999999):06d}"
        phone_number = phone_data.get('display_phone_number')
        normalized_phone_number = _normalize_phone_number(phone_number)
        if target_phone_id and str(phone_id) != target_phone_id:
            continue
        if target_phone_number and normalized_phone_number != target_phone_number:
            continue
        if target_phone_id or target_phone_number:
            matched_target = True

        biz_data = utils.call_api(waba=waba, endpoint=f"{phone_id}?fields=is_on_biz_app,platform_type")
        is_on_biz_app = biz_data.get("is_on_biz_app")

        if is_on_biz_app and biz_data.get("platform_type") == "CLOUD_API":
            phone_type = "app"
        elif register:
            payload = {
                'messaging_product': 'whatsapp',
                'pin': pin
            }
            try:
                utils.call_api(waba=waba, endpoint=f"{phone_id}/register", method="post", payload=payload)
            except TRANSIENT_ERRORS:
                raise
            except Exception as e:
                error_data = e.args[0] if e.args and isinstance(e.args[0], dict) else {}
                error = error_data.get("error") if isinstance(error_data, dict) else {}
                if isinstance(error, dict) and error.get("code") == 133005:
                    logger.warning(
                        "WABA phone register skipped by PIN mismatch: waba_id=%s phone_id=%s",
                        waba.waba_id,
                        phone_id,
                    )
                else:
                    raise

        phone = Phone.objects.filter(phone=normalized_phone_number).first()
        if phone:
            update_fields = []
            if phone.phone_id != phone_id:
                phone.phone_id = phone_id
                update_fields.append("phone_id")
            if phone.waba_id != waba.id:
                phone.waba = waba
                update_fields.append("waba")
            if user and not phone.owner_id:
                phone.owner = user
                update_fields.append("owner")
            if update_fields:
                phone.save(update_fields=update_fields)
        else:
            phone, _created = Phone.objects.get_or_create(
                phone_id=phone_id,
                defaults={
                    "waba": waba,
                    "owner": user,
                    "phone": normalized_phone_number,
                    "pin": pin,
                    "sms_service": True,
                    "ChatFromSms": False,
                    "type": phone_type,
                }
            )

        if user and create_lead:
            # create lead in b24
            from separator.bitrix.tasks import prepare_lead
            prepare_lead.delay(user.id, f'New WhatsApp Cloud: {phone_number}')

        if user and "separator.tariff" in settings.INSTALLED_APPS and not phone.date_end:
            from separator.tariff.utils import get_trial
            phone.date_end = get_trial(user, "waba")
            phone.save()

    if (target_phone_id or target_phone_number) and not matched_target:
        target = target_phone_id or target_phone_number
        raise Exception(f"Phone {target} not found in WABA {waba.waba_id}")


@shared_task(queue='waba', **RETRY_KWARGS)
def force_sync_waba_phones(waba_id):
    waba = Waba.objects.select_related("app", "owner").get(id=waba_id)
    _onboard_waba_assets(
        waba,
        user=waba.owner,
        register=False,
        save_templates=False,
        create_lead=False,
    )


@shared_task(queue='waba', **RETRY_KWARGS)
def transcribe_voice_message(
    phone_id,
    app_instance_id,
    user_phone,
    connector_code,
    line_id,
    media_url,
    filename,
    message_id=None,
    push_name=None,
    media_id=None,
):
    phone = None
    input_path = None
    converted_path = None
    try:
        phone = Phone.objects.select_related("waba", "waba__app").get(id=phone_id)
        app = phone.waba.app if phone.waba_id and phone.waba else None
        if not app or not app.openai_api_key:
            return None
        if phone.tokens <= 0:
            return None

        if media_id:
            media_info = utils.call_api(waba=phone.waba, endpoint=media_id)
            media_url = media_info.get("url") if isinstance(media_info, dict) else media_url

        media_response = utils.call_api(file_url=media_url, waba=phone.waba)
        media_response.raise_for_status()

        original_ext = os.path.splitext(filename or "")[1] or ".oga"
        with tempfile.NamedTemporaryFile(suffix=original_ext, delete=False) as input_file:
            input_path = input_file.name
            input_file.write(media_response.content)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as converted_file:
            converted_path = converted_file.name

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                converted_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        with open(converted_path, "rb") as audio_file:
            openai_base_url = settings.OPENAI_API_BASE_URL.rstrip("/")
            response = requests.post(
                f"{openai_base_url}/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {app.openai_api_key}"},
                data={"model": phone.transcribe_model},
                files={"file": ("voice.mp3", audio_file)},
                timeout=(10, 120),
            )
        response.raise_for_status()
        data = response.json()
        text = (data.get("text") or "").strip()
        if not text:
            return None

        usage = data.get("usage") or {}
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, int) and total_tokens > 0:
            Phone.objects.filter(id=phone.id).update(
                tokens=models.Case(
                    models.When(tokens__gte=total_tokens, then=models.F("tokens") - total_tokens),
                    default=0,
                    output_field=models.PositiveIntegerField(),
                )
            )

        return bitrix_tasks.send_messages(
            app_instance_id,
            user_phone,
            f"Transcript ({total_tokens} tokens):\n{text}",
            connector_code,
            line_id,
            pushName=push_name,
            message_id=f"{message_id}:transcription" if message_id else None,
        )
    except Exception as e:
        try:
            bitrix_tasks.send_messages.delay(
                app_instance_id,
                user_phone,
                f"[color=#ff0000]Transcription error: {e}[/color]",
                connector_code,
                line_id,
                pushName=push_name,
                message_id=f"{message_id}:transcription:error" if message_id else None,
                manager_id=0,
            )
        except Exception:
            pass
        raise
    finally:
        for path in (input_path, converted_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


# https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
def exchange_embedded_signup_code(request_id, app_id, include_redirect_uri=True):
    current_data = redis_client.json().get(request_id, "$")
    current_data = current_data[0]
    app = App.objects.filter(client_id=app_id).first()
    host = current_data.get("host") or app.sites.values_list("domain", flat=True).first()

    payload = {
        "client_id": app.client_id,
        "client_secret": app.client_secret,
        "code": current_data.get('code'),
    }
    if include_redirect_uri:
        payload["redirect_uri"] = f'https://{host}/waba/callback/'

    response = utils.call_api(app=app, endpoint="oauth/access_token", method="post", payload=payload)
    access_token = response.get('access_token')
    debug_token = utils.call_api(app=app, endpoint=f"debug_token?input_token={access_token}")
    token_data = debug_token.get('data', {})
    granular_scopes = token_data.get('granular_scopes', {})
    wabas = next((item['target_ids'] for item in granular_scopes if item['scope'] == 'whatsapp_business_management'), None)
    return current_data, app, access_token, wabas


def _upsert_waba(app, access_token, waba_id, owner=None, partner_app=None, subscribed=None):
    if subscribed is None:
        subscribed = app.subscribe

    updated = False
    waba, created = Waba.objects.get_or_create(
        waba_id=waba_id,
        defaults={
            'app': app,
            'partner_app': partner_app,
            'access_token': access_token,
            'owner': owner,
            'subscribed': subscribed,
        }
    )
    if not created:
        update_fields = []
        if waba.app_id != app.id:
            waba.app = app
            update_fields.append("app")
        partner_app_id = getattr(partner_app, "id", None)
        if waba.partner_app_id != partner_app_id:
            waba.partner_app = partner_app
            update_fields.append("partner_app")
        if waba.access_token != access_token:
            waba.access_token = access_token
            update_fields.append("access_token")
        owner_id = getattr(owner, "id", None)
        if waba.owner_id != owner_id:
            waba.owner = owner
            update_fields.append("owner")
        if waba.subscribed != subscribed:
            waba.subscribed = subscribed
            update_fields.append("subscribed")
        if update_fields:
            waba.save(update_fields=update_fields)
            updated = True
    return waba, created, updated


def onboard_embedded_signup_waba(app, access_token, user, waba_id, target_phone_id=None):
    waba, _created, _updated = _upsert_waba(app, access_token, waba_id, owner=user)
    _onboard_waba_assets(waba, user=user, register=app.register, target_phone_id=target_phone_id)
    return waba


@shared_task(queue='waba', **RETRY_KWARGS)
def add_popup_phone(request_id, app_id):
    current_data, app, access_token, wabas = exchange_embedded_signup_code(
        request_id,
        app_id,
        include_redirect_uri=False,
    )
    session = current_data.get("popup_session") or {}
    waba_id = session.get("waba_id")
    phone_number_id = session.get("phone_number_id")
    if not waba_id:
        raise Exception("Popup session WABA ID is missing")
    if not phone_number_id:
        raise Exception("Popup session phone number ID is missing")
    if wabas and waba_id not in wabas:
        raise Exception("Popup WABA ID is not available in exchanged token")

    user = User.objects.get(id=current_data.get('user'))
    waba = onboard_embedded_signup_waba(
        app,
        access_token,
        user,
        waba_id,
        target_phone_id=phone_number_id,
    )
    return {"waba_id": waba.waba_id, "phone_number_id": phone_number_id}


@shared_task(queue='waba', **RETRY_KWARGS)
def add_waba_phone(request_id, app_id):
    current_data, app, access_token, wabas = exchange_embedded_signup_code(request_id, app_id)
    
    if wabas:
        user = User.objects.get(id=current_data.get('user'))
        for waba_id in wabas:
            onboard_embedded_signup_waba(app, access_token, user, waba_id)


@shared_task(queue='waba', **RETRY_KWARGS)
def add_partner_waba_phone(request_id, app_id):
    current_data = redis_client.json().get(request_id, "$")
    current_data = current_data[0]
    app = App.objects.filter(client_id=app_id).first()
    partner_app = PartnerApp.objects.select_related("owner").filter(
        id=current_data.get("partner_app_id"),
        app=app,
        active=True,
    ).first()
    if not partner_app:
        raise Exception("Partner app not found")

    access_token = current_data.get("access_token")
    wabas = current_data.get("wabas") or []
    if not access_token or not wabas:
        raise Exception("Partner signup data is missing")

    waba_id = wabas[0]
    waba, created, updated = _upsert_waba(
        app,
        access_token,
        waba_id,
        owner=partner_app.owner,
        partner_app=partner_app,
        subscribed=True,
    )
    queue_subscription = not created and not updated

    _onboard_waba_assets(waba, user=partner_app.owner, register=app.register)
    if queue_subscription:
        waba_subscription.delay(waba.id)


@shared_task(queue='waba', **RETRY_KWARGS)
def hosted_partner_added(app_id, waba_id, owner_business_id):
    app = App.objects.filter(id=app_id, auth_flow=App.AuthFlow.HOSTED).first()
    if not app:
        raise Exception(f"Hosted app not found: {app_id}")
    if not waba_id or not owner_business_id:
        raise Exception(f"Hosted payload is missing waba_id or owner_business_id: {waba_id}, {owner_business_id}")

    access_token = utils.get_hosted_business_token(app, owner_business_id)
    waba, _created, _updated = _upsert_waba(app, access_token, waba_id)
    _onboard_waba_assets(waba, user=None, register=app.register)


@shared_task(queue='waba', **RETRY_KWARGS)
def add_phone_number_to_waba(waba_id, phone_number):
    waba = Waba.objects.select_related("app", "owner").filter(waba_id=waba_id).first()
    if not waba:
        raise Exception(f"WABA not found: {waba_id}")
    if not waba.app:
        raise Exception(f"App not found for WABA: {waba_id}")

    _onboard_waba_assets(
        waba,
        user=waba.owner,
        register=False,
        target_phone_number=phone_number,
        save_templates=False,
        create_lead=True,
    )


@shared_task(queue='waba', **RETRY_KWARGS)
def register_phone(phone_id):
    phone = Phone.objects.select_related("waba").filter(id=phone_id).first()
    if not phone or not phone.waba:
        raise Exception(f"Phone not found or has no WABA: {phone_id}")

    payload = {
        "messaging_product": "whatsapp",
        "pin": phone.pin,
    }
    return utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/register", method="post", payload=payload)


@shared_task(queue='waba', **RETRY_KWARGS)
def send_single_message(template, recipient, id, components=None, broadcast_id=None):
    try:
        phone = Phone.objects.get(id=id)
        tmp = Template.objects.get(id=template)
        is_marketing_template = (tmp.category or "").upper() == "MARKETING"
        if broadcast_id:
            from .models import TemplateBroadcast
            broadcast = TemplateBroadcast.objects.filter(id=broadcast_id).first()
            if broadcast and broadcast.status == "cancelled":
                return None
        payload = {
            "messaging_product": "whatsapp",
            "type": "template",
            "to": recipient,
            "template": {
                "name": tmp.name,
                "language": {"code": tmp.lang},
            }
        }
        if components:
            payload["template"]["components"] = components
        try:
            endpoint = f"{phone.phone_id}/marketing_messages" if is_marketing_template else f"{phone.phone_id}/messages"
            resp = utils.call_api(waba=phone.waba, endpoint=endpoint, method="post", payload=payload)
            message_id = None
            messages = resp.get("messages") or []
            if messages:
                message_id = messages[0].get("id")
            if broadcast_id:
                from .models import TemplateBroadcastRecipient, TemplateBroadcast
                TemplateBroadcastRecipient.objects.filter(
                    broadcast_id=broadcast_id,
                    recipient_phone=recipient,
                ).update(
                    wamid=message_id,
                    status="sent" if message_id else "failed",
                    error_json=None,
                )
                TemplateBroadcast.objects.filter(id=broadcast_id).update(status="sent")
            return resp
        except TRANSIENT_ERRORS:
            raise
        except Exception as e:
            err = None
            if e.args and isinstance(e.args[0], dict):
                err = e.args[0]
            if broadcast_id:
                from .models import TemplateBroadcastRecipient, TemplateBroadcast
                TemplateBroadcastRecipient.objects.filter(
                    broadcast_id=broadcast_id,
                    recipient_phone=recipient,
                ).update(status="failed", error_json=err or {"error": str(e)})
                TemplateBroadcast.objects.filter(id=broadcast_id).update(status="failed")
                return err or {"error": str(e)}
            raise
    except Exception:
        raise


@shared_task(queue='waba', **RETRY_KWARGS)
def send_message(template, recipients, id, components=None, broadcast_id=None):
    task_ids = []
    for recipient in recipients:
        task = send_single_message.delay(
            template,
            recipient,
            id,
            components=components,
            broadcast_id=broadcast_id,
        )
        task_ids.append({
            "recipient": recipient,
            "task_id": task.id,
        })
    return {
        "scheduled": len(task_ids),
        "tasks": task_ids,
    }

@shared_task(queue='waba', **RETRY_KWARGS)
def call_management(id):
    phone = Phone.objects.filter(id=id).first()
    payload = {
        "calling": {
            "status": phone.calling,
            "srtp_key_exchange_protocol": phone.srtp_key_exchange_protocol,
            "callback_permission_status": phone.callback_permission_status,
            "sip": {
                "status": phone.sip_status,
                "servers": [
                    {
                        "hostname": phone.sip_hostname,
                        "port": phone.sip_port
                    }
                ]
            }
        }
    }
    try:
        resp = utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/settings", method="post", payload=payload)
        try:
            if phone.calling == "enabled":
                sip_cred = utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/settings?include_sip_credentials=true")
                calling_data = sip_cred.get("calling", {})
                sip_servers = calling_data.get("sip", {}).get("servers", [])

                matched_server = None
                for server in sip_servers:
                    if (
                        server.get("hostname") == phone.sip_hostname
                        and str(server.get("port")) == str(phone.sip_port)
                    ):
                        matched_server = server
                        break

                if not matched_server:
                    matched_server = next((server for server in sip_servers if server.get("sip_user_password")), None)

                sip_password = matched_server.get("sip_user_password") if matched_server else None
                if not sip_password:
                    raise Exception({"error": "SIP password not found", "settings": sip_cred})

                phone.sip_user_password = sip_password
                phone.save(update_fields=["sip_user_password"])

            return resp

        except ValueError:
            raise

    except TRANSIENT_ERRORS:
        raise
    except Exception as e:
        raw_error = e.args[0] if e.args else None
        raise Exception(raw_error or str(e)) from e


@shared_task(queue='waba', **RETRY_KWARGS)
def waba_subscription(waba_id):
    waba = Waba.objects.select_related("app", "partner_app").filter(id=waba_id).first()
    if not waba or not waba.app:
        return
    if not waba.app.subscribe and not waba.subscribed:
        return {"status": "skipped", "reason": "app subscription disabled"}

    method = "post" if waba.subscribed else "delete"
    payload = None
    if method == "post" and waba.partner_app_id and waba.partner_app.active:
        payload = {
            "override_callback_uri": waba.partner_app.webhook_url,
            "verify_token": waba.partner_app.verify_token,
        }
    return utils.call_api(waba=waba, endpoint=f"{waba.waba_id}/subscribed_apps", method=method, payload=payload)


@shared_task(queue='waba', **RETRY_KWARGS)
def delete_template(template_id, owner_id=None):
    template = Template.objects.filter(id=template_id).first()
    if not template:
        return {
            "status": "not_found",
            "template_id": template_id,
        }

    if owner_id and template.owner_id != owner_id:
        raise Exception(f"Template {template_id} does not belong to user {owner_id}")

    remote_result = utils.delete_template_remote(template)
    if isinstance(remote_result, dict) and remote_result.get("error"):
        raise Exception(str(remote_result))

    template.delete()
    return remote_result

# https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging/#ads-that-click-to-whatsapp
@shared_task(queue='waba', **RETRY_KWARGS)
def send_ctwa_conversion(ctwa_id, event="Purchase", custom_data=None):
    try:
        ctwa = Ctwa.objects.select_related('waba').get(id=ctwa_id)
    except Ctwa.DoesNotExist:
        raise Exception(f"Ctwa with id {ctwa_id} does not exist")
        
    waba = ctwa.waba
    
    if not waba.dataset:
        try:
            resp = utils.call_api(waba=waba, endpoint=f"{waba.waba_id}/dataset", method="post", payload={})
            dataset_id = None
            if isinstance(resp, dict):
                if 'data' in resp and isinstance(resp['data'], list) and len(resp['data']) > 0:
                    dataset_id = resp['data'][0].get('id')
                elif 'id' in resp:
                    dataset_id = resp['id']
            
            if dataset_id:
                waba.dataset = int(dataset_id)
                waba.save(update_fields=['dataset'])
            else:
                raise Exception(f"Dataset for WABA {waba.waba_id} was not returned by Facebook")
        except TRANSIENT_ERRORS:
            raise
        except Exception:
            raise

    if custom_data is None:
        custom_data = {
            "currency": "USD",
            "value": 0,
        }
        
    payload = {
        "data": [
            {
                "event_name": event,
                "event_time": int(time.time()),
                "action_source": "business_messaging",
                "messaging_channel": "whatsapp",
                "user_data": {
                    "whatsapp_business_account_id": waba.waba_id,
                    "ctwa_clid": ctwa.clid
                },
                "custom_data": custom_data
            }
        ],
        "partner_agent": waba.app.name if waba.app else "separator.biz"
    }
    
    try:
        resp = utils.call_api(waba=waba, endpoint=f"{waba.dataset}/events", method="post", payload=payload)
        CtwaEvents.objects.create(
            ctwa=ctwa,
            date=timezone.now(),
            event=event,
        )
        # ctwa.delete()
        return resp
    except TRANSIENT_ERRORS:
        raise
    except Exception:
        raise

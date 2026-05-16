import time
import redis
import random
from celery import shared_task

from .models import App, Waba, Phone, Template, Error, Ctwa, CtwaEvents
import separator.waba.utils as utils

from separator.users.models import User

from django.conf import settings
from django.utils import timezone


redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def _normalize_phone_number(phone_number):
    return '+' + ''.join(filter(str.isdigit, phone_number or ''))


def _onboard_waba_assets(waba, user=None, register=True, target_phone_number=None, save_templates=True, create_lead=True):
    # Common post-token onboarding for Embedded Signup and Hosted Embedded Signup.
    if save_templates:
        utils.save_approved_templates.delay(waba.id)

    try:
        resp = utils.call_api(waba=waba, endpoint=f"{waba.waba_id}/phone_numbers")
        phone_numbers = resp.get('data', {})
    except Exception:
        raise

    target_phone_number = _normalize_phone_number(target_phone_number) if target_phone_number else None
    matched_target = False

    for phone_data in phone_numbers:
        phone_type = "cloud"
        phone_id = phone_data.get('id')
        pin = f"{random.randint(0, 999999):06d}"
        phone_number = phone_data.get('display_phone_number')
        normalized_phone_number = _normalize_phone_number(phone_number)
        if target_phone_number and normalized_phone_number != target_phone_number:
            continue
        matched_target = True

        try:
            biz_data = utils.call_api(waba=waba, endpoint=f"{phone_id}?fields=is_on_biz_app,platform_type")
            is_on_biz_app = biz_data.get("is_on_biz_app")
        except Exception:
            raise

        if is_on_biz_app and biz_data.get("platform_type") == "CLOUD_API":
            phone_type = "app"
        elif register:
            payload = {
                'messaging_product': 'whatsapp',
                'pin': pin
            }
            try:
                utils.call_api(waba=waba, endpoint=f"{phone_id}/register", method="post", payload=payload)
            except Exception:
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
            phone, created = Phone.objects.get_or_create(
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

    if target_phone_number and not matched_target:
        raise Exception(f"Phone number {target_phone_number} not found in WABA {waba.waba_id}")


# https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
@shared_task(queue='waba')
def add_waba_phone(request_id, app_id):
    
    current_data = redis_client.json().get(request_id, "$")
    current_data = current_data[0]
    app = App.objects.filter(client_id=app_id).first()
    host = current_data.get("host") or app.sites.values_list("domain", flat=True).first()

    payload = {
        "client_id": app.client_id,
        "client_secret": app.client_secret,
        "code": current_data.get('code'),
        "redirect_uri": f'https://{host}/waba/callback/'
    }

    try:
        response = utils.call_api(app=app, endpoint="oauth/access_token", method="post", payload=payload)
        access_token = response.get('access_token')
    except Exception:
        raise
    wabas = None
    try:
        debug_token = utils.call_api(app=app, endpoint=f"debug_token?input_token={access_token}")
        token_data = debug_token.get('data', {})
        granular_scopes = token_data.get('granular_scopes', {})
        wabas = next((item['target_ids'] for item in granular_scopes if item['scope'] == 'whatsapp_business_management'), None)
    except Exception:
        raise
    
    if wabas:
        user = User.objects.get(id=current_data.get('user'))
        for waba_id in wabas:
            waba, created = Waba.objects.get_or_create(
                waba_id=waba_id,
                app=app,
                defaults={
                    'access_token': access_token,
                    'owner': user,
                    'subscribed': app.subscribe,
                }
            )
            if not created and waba.access_token != access_token:
                waba.access_token = access_token
                waba.save(update_fields=["access_token"])
            _onboard_waba_assets(waba, user=user, register=app.register)


@shared_task(queue='waba')
def hosted_partner_added(app_id, waba_id, owner_business_id):
    app = App.objects.filter(id=app_id, hosted=True).first()
    if not app:
        raise Exception(f"Hosted app not found: {app_id}")
    if not waba_id or not owner_business_id:
        raise Exception(f"Hosted payload is missing waba_id or owner_business_id: {waba_id}, {owner_business_id}")

    access_token = utils.get_hosted_business_token(app, owner_business_id)
    waba, created = Waba.objects.get_or_create(
        waba_id=waba_id,
        defaults={
            "app": app,
            "access_token": access_token,
            "owner": None,
            "subscribed": app.subscribe,
        }
    )

    update_fields = []
    if not created:
        if waba.app_id != app.id:
            waba.app = app
            update_fields.append("app")
        if waba.access_token != access_token:
            waba.access_token = access_token
            update_fields.append("access_token")
        if waba.subscribed != app.subscribe:
            waba.subscribed = app.subscribe
            update_fields.append("subscribed")
        if update_fields:
            waba.save(update_fields=update_fields)

    _onboard_waba_assets(waba, user=None, register=app.register)


@shared_task(queue='waba')
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


@shared_task(queue='waba')
def register_phone(phone_id):
    phone = Phone.objects.select_related("waba").filter(id=phone_id).first()
    if not phone or not phone.waba:
        raise Exception(f"Phone not found or has no WABA: {phone_id}")

    payload = {
        "messaging_product": "whatsapp",
        "pin": phone.pin,
    }
    return utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/register", method="post", payload=payload)


@shared_task(queue='waba')
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


@shared_task(queue='waba')
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

@shared_task(queue='waba')
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
            if phone.error:
                phone.error = None
                phone.save()
         
            if phone.calling == "enabled":
                sip_cred = utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/settings?include_sip_credentials=true")
                
                # Получаем и сохраняем SIP пароль
                calling_data = sip_cred.get("calling", {})
                sip_servers = calling_data.get("sip", {}).get("servers", [])
                
                # Ищем сервер с нужным app_id
                if not phone.waba.app:
                    raise Exception("App not found")
                app_id = phone.waba.app.client_id
                for server in sip_servers:
                    if server.get("app_id") == int(app_id):
                        sip_password = server.get("sip_user_password")
                        if sip_password:
                            phone.sip_user_password = sip_password
                            phone.save()
                        break
                else:
                    raise Exception(f"No matching server found for app_id {app_id}")
                
            return resp

        except ValueError:
            raise

    except Exception as e:
        error = None
        if e.args and isinstance(e.args[0], dict):
            error = e.args[0].get("error")
        
        if error:
            code = error.get("code")
        if code:
            fb_message = error.get("error_user_title")
            fb_details = error.get("error_user_msg")
            error_obj, created = Error.objects.get_or_create(
                code=code,
                defaults={"message": fb_message, "details": fb_details}
            )
            error_text = f"Error code: {code}. {error_obj.message}. {error_obj.details}"
        else:
            error_text = str(e)
        phone.error = error_text
        phone.save()
        raise Exception(phone.phone_id, e)


@shared_task(queue='waba')
def waba_subscription(waba_id):
    waba = Waba.objects.select_related("app").filter(id=waba_id).first()
    if not waba or not waba.app:
        return
    if not waba.app.subscribe and not waba.subscribed:
        return {"status": "skipped", "reason": "app subscription disabled"}

    method = "post" if waba.subscribed else "delete"
    return utils.call_api(app=waba.app, endpoint=f"{waba.waba_id}/subscribed_apps", method=method)


@shared_task(queue='waba')
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
@shared_task(queue='waba')
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
    except Exception:
        raise

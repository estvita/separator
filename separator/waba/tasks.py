import redis
import random
from celery import shared_task

from .models import App, Waba, Phone, Template, Error
import separator.waba.utils as utils

from separator.users.models import User

from django.conf import settings


redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)

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
    except Exception as e:
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
                }
            )
            # get templates
            utils.save_approved_templates.delay(waba.id)

            # get phones
            try:
                resp = utils.call_api(waba=waba, endpoint=f"{waba_id}/phone_numbers")
                phone_numbers = resp.get('data', {})
            except Exception:
                raise
            
            for phone in phone_numbers:
                phone_type = "cloud"
                phone_id = phone.get('id')
                pin = f"{random.randint(0, 999999):06d}"
                phone_number = phone.get('display_phone_number')
                # https://developers.facebook.com/docs/whatsapp/embedded-signup/custom-flows/onboarding-business-app-users
                try:
                    biz_data = utils.call_api(waba=waba, endpoint=f"{phone_id}?fields=is_on_biz_app,platform_type")
                    is_on_biz_app = biz_data.get("is_on_biz_app")
                except Exception:
                    raise
                if is_on_biz_app and biz_data.get("platform_type") == "CLOUD_API":
                    phone_type = "app"
                    pass
                else:
                    payload = {
                        'messaging_product': 'whatsapp',
                        'pin': pin
                    }
                    try:
                        resp = utils.call_api(waba=waba, endpoint=f"{phone_id}/register", method="post", payload=payload)
                    except Exception:
                        raise

                phone, created = Phone.objects.get_or_create(
                    phone_id=phone_id,
                    defaults={
                        "waba": waba,
                        "owner": user,
                        "phone": phone_number,
                        "pin": pin,
                        "type": phone_type,
                    }
                )

                # create lead in b24
                if not user.integrator:
                    from separator.bitrix.tasks import prepare_lead
                    prepare_lead.delay(user.id, f'New WhatsApp Cloud: {phone_number}')

                if "separator.tariff" in settings.INSTALLED_APPS and not phone.date_end:
                    from separator.tariff.utils import get_trial
                    phone.date_end = get_trial(user, "waba")
                    phone.save()
                        
@shared_task(queue='waba')
def send_message(template, recipients, id, components=None, broadcast_id=None):
    try:
        phone = Phone.objects.get(id=id)
        tmp = Template.objects.get(id=template)
        if broadcast_id:
            from .models import TemplateBroadcast
            broadcast = TemplateBroadcast.objects.filter(id=broadcast_id).first()
            if broadcast and broadcast.status == "cancelled":
                return
        for recipient in recipients:
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
                resp = utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/messages", method="post", payload=payload)
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
            except Exception as e:
                if broadcast_id:
                    from .models import TemplateBroadcastRecipient, TemplateBroadcast
                    err = None
                    if e.args and isinstance(e.args[0], dict):
                        err = e.args[0]
                    TemplateBroadcastRecipient.objects.filter(
                        broadcast_id=broadcast_id,
                        recipient_phone=recipient,
                    ).update(status="failed", error_json=err or {"error": str(e)})
                    TemplateBroadcast.objects.filter(id=broadcast_id).update(status="failed")
                else:
                    raise
    except Exception:
        raise

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

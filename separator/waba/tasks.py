import re
import redis
import random
import requests
from celery import shared_task
from django.shortcuts import  get_object_or_404

from .models import Waba, Phone, Template
import separator.waba.utils as utils
import separator.chatwoot.utils as chatwoot
from separator.chatwoot.models import Inbox

from separator.users.models import User

from django.conf import settings


redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

# https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
@shared_task(queue='waba')
def add_waba_phone(request_id):
    app = utils.get_app()
    if not app:
        raise
    
    current_data = redis_client.json().get(request_id, "$")
    current_data = current_data[0]

    payload = {
        "client_id": app.client_id,
        "client_secret": app.client_secret,
        "code": current_data.get('code'),
        "redirect_uri": f'https://{app.site}/waba/callback/'
    }

    try:
        response = utils.call_api(endpoint="oauth/access_token", method="post", payload=payload)
        access_token = response.json().get('access_token')
    except Exception as e:
        raise
    wabas = None
    try:
        debug_token = utils.call_api(endpoint=f"debug_token?input_token={access_token}")
        token_data = debug_token.json().get('data', {})
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
                phone_numbers = resp.json().get('data', {})
            except Exception:
                raise
            
            for phone in phone_numbers:
                phone_id = phone.get('id')
                phone_number = phone.get('display_phone_number')
                pin = f"{random.randint(0, 999999):06d}"

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
                    }
                )

                if "separator.tariff" in settings.INSTALLED_APPS and not phone.date_end:
                    from separator.tariff.utils import get_trial
                    phone.date_end = get_trial(user, "waba")
                    phone.save()

                if settings.CHATWOOT_ENABLED and not phone.inbox:
                    # add phone to chatwoot
                    cleaned_number = re.sub(r'[^\d+]', '', phone_number)
                    inbox_data = {
                        'name': cleaned_number,
                        'lock_to_single_conversation': True,
                        'channel': {
                            'phone_number': cleaned_number,
                            'provider': 'whatsapp_cloud',
                            'type': 'whatsapp',
                            'provider_config': {
                                'api_key': access_token,
                                'business_account_id': waba_id,
                                'phone_number_id': phone_id
                            }
                        }
                    }
                    resp = chatwoot.add_inbox(user, inbox_data)
                    if "result" in resp:
                        result = resp.get('result', {})
                        try:
                            inbox, created = Inbox.objects.update_or_create(
                                owner=user,
                                id=result['inbox_id'],  # Уникальный идентификатор inbox
                                defaults={'account': result['account']}
                            )
                            phone.inbox = inbox
                            phone.save()
                        except Inbox.MultipleObjectsReturned:
                            raise Exception(f"Multiple Inboxes found for owner {user} and id {result['inbox_id']}")
                        
            # subscribed_apps
            try:
                utils.call_api(endpoint=f"{waba_id}/subscribed_apps", method="post")
            except Exception:
                raise

@shared_task(queue='waba')
def send_message(template, recipients, phone_id):
    phone = get_object_or_404(Phone, id=phone_id)
    tmp = Template.objects.get(id=template)
    for recipient in recipients:
        payload = {
            "type": "template",
            "to": recipient,
            "template": {
                "name": tmp.name,
                "language": {"code": tmp.lang},
            }
        }
        utils.call_api(waba=phone.waba, endpoint=f"{phone_id}/messages", method="post", payload=payload)


@shared_task(queue='waba')
def call_management(id):
    phone = get_object_or_404(Phone, id=id)
    servers = []
    if phone.calling == "enabled":
        servers.append({
            "hostname": phone.sip_hostname,
            "port": phone.sip_port
        })
    payload = {
        "calling": {
            "status": phone.calling,
            "srtp_key_exchange_protocol": phone.srtp_key_exchange_protocol,
            "callback_permission_status": phone.callback_permission_status,
            "sip": {
                "status": phone.sip_status,
                "servers": servers
            }
        }
    }
    try:
        resp = utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/settings", method="post", payload=payload)
        resp.raise_for_status()
        resp = resp.json()
        try:
            if phone.error:
                phone.error = None
                phone.save()
         
            if phone.calling == "enabled":
                sip_cred = utils.call_api(waba=phone.waba, endpoint=f"{phone.phone_id}/settings?include_sip_credentials=true")
                sip_cred.raise_for_status()
                sip_cred = sip_cred.json()
                
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

    except requests.exceptions.HTTPError:
        resp = resp.json()
        if "error" in resp:
            error = resp.get("error", {})
            message = error.get("message")
            code = error.get("code")
            phone.error = f"{code}: {message}"
            phone.save()
        raise Exception(phone.phone_id, resp)

    except requests.exceptions.RequestException as e:
        raise Exception(e)
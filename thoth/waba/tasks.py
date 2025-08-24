from celery import shared_task
import redis
import re
import requests
from django.shortcuts import  get_object_or_404

from .models import App, Waba, Phone, Template
import thoth.waba.utils as utils
import thoth.chatwoot.utils as chatwoot
from thoth.chatwoot.models import Inbox

from thoth.users.models import User

from django.conf import settings
WABA_APP_ID = settings.WABA_APP_ID
API_URL = settings.FACEBOOK_API_URL
apps = settings.INSTALLED_APPS

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

def get_app():
    return get_object_or_404(App, id=WABA_APP_ID)    

@shared_task(queue='waba')
def add_waba_phone(current_data, code, request_id):
    app = get_app()
    if not app:
        raise
    
    base_url = f"{API_URL}/v{app.api_version}.0/"

    current_data = current_data[0]

    user = User.objects.get(id=current_data.get('user'))
    current_data.update({'code': code})
    redis_client.json().set(request_id, "$", current_data)

    payload = {
        'client_id': app.client_id,
        'client_secret': app.client_secret,
        'redirect_uri': f'https://{app.site}/waba/callback/',
        'code': current_data.get('code'),
    }

    url = f"{base_url}oauth/access_token"
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        raise Exception('access_token', response.json())

    access_token = response.json().get('access_token')
    headers = {"Authorization": f"Bearer {access_token}"}

    redis_client.json().set(request_id, "$.access_token", access_token)

    app_headers = {"Authorization": f"Bearer {app.access_token}"}
    debug_token = requests.get(f"{base_url}debug_token?input_token={access_token}", headers=app_headers)
    if debug_token.status_code != 200:
        raise Exception('debug_token', debug_token.json())
    
    token_data = debug_token.json().get('data', {})
    granular_scopes = token_data.get('granular_scopes', {})
    wabas = next((item['target_ids'] for item in granular_scopes if item['scope'] == 'whatsapp_business_management'), None)
    if wabas:
        for waba_id in wabas:
            waba, created = Waba.objects.get_or_create(
                waba_id=waba_id)

            waba.access_token = access_token
            waba.owner = user
            waba.save()

            # get templates
            url = f"{base_url}{waba_id}/message_templates"
            template_resp = requests.get(url, headers=headers)
            if template_resp.status_code != 200:
                raise Exception('template_resp', template_resp.json())
            templates_data = template_resp.json()
            utils.save_approved_templates(waba, user, templates_data)
            
            # subscribe app
            url = f"{base_url}{waba_id}/subscribed_apps"
            subs_resp = requests.post(url, json=payload, headers=headers)
            if subs_resp.status_code != 200:
                print('subscribed_apps', subs_resp.json())
            utils.sample_template(access_token, waba_id)

            # get phones 
            resp = requests.get(f"{base_url}{waba_id}/phone_numbers?access_token={access_token}", headers=app_headers)
            if resp.status_code != 200:
                raise Exception('phone_numbers', resp.json())
            
            phone_numbers = resp.json().get('data', {})
            for phone in phone_numbers:
                phone_id = phone.get('id')
                phone_number = phone.get('display_phone_number')

                phone, created = Phone.objects.get_or_create(phone_id=phone_id)
                phone.waba = waba
                phone.owner = user
                phone.phone = phone_number
                if "thoth.tariff" in apps and not phone.date_end:
                    from thoth.tariff.utils import get_trial
                    phone.date_end = get_trial(user, "waba")
                phone.save()

                payload = {
                    'messaging_product': 'whatsapp',
                    'pin': '000000'
                }
                url = f"{base_url}{phone_id}/register"
                resp = requests.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise Exception('register', resp.json())

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
                if settings.CHATWOOT_ENABLED and not phone.inbox:
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


@shared_task(queue='waba')
def send_message(template, recipients, phone_id):
    phone = get_object_or_404(Phone, id=phone_id)
    tmp = Template.objects.get(id=template)
    access_token = phone.waba.access_token
    message = {
        "type": "template",
        "template": {
            "name": tmp.name,
            "language": {"code": tmp.lang},
        }
    }
    for recipient in recipients:
        utils.send_whatsapp_message(access_token, phone.phone_id, recipient, message)


@shared_task(queue='waba')
def call_management(id):
    app = get_app()
    if not app:
        raise
    base_url = f"{API_URL}/v{app.api_version}.0"
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
            "sip": {
                "status": phone.sip_status,
                "servers": servers
            }
        }
    }
    headers = {"Authorization": f"Bearer {phone.waba.access_token}"}
    try:
        resp = requests.post(
            f"{base_url}/{phone.phone_id}/settings",
            json=payload,
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        try:
            if phone.error:
                phone.error = None
                phone.save()
            return resp.json()
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
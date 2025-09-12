from django.shortcuts import render, redirect, get_object_or_404

from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.db import transaction
from django.db.models import Q

import logging
import redis
import requests
import json
import uuid
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponseBadRequest, HttpResponseServerError, HttpResponse
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from urllib.parse import urlencode
from thoth.decorators import login_message_required, user_message

import thoth.bitrix.utils as bitrix_utils
import thoth.bitrix.tasks as bitrix_tasks

from thoth.users.models import User, Message
from thoth.bitrix.models import Line

from .models import App, Waba, Phone, Template
import thoth.waba.utils as waba_utils
import thoth.waba.tasks as waba_tasks

from thoth.freepbx.tasks import create_extension_task

API_URL = settings.FACEBOOK_API_URL
WABA_APP_ID = settings.WABA_APP_ID

logger = logging.getLogger(__name__)
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

def check_template_exists(access_token, waba_id, template_name):
    app = App.objects.get(id=WABA_APP_ID)
    url = f"{API_URL}/v{app.api_version}.0/{waba_id}/message_templates"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        templates = response.json().get('data', [])
        for template in templates:
            if template['name'] == template_name:
                return True
        return False
    else:
        print(f"Error fetching templates: {response.status_code}, {response.json()}")
        return False

def delete_voximplant(phone):
    if phone.voximplant_id and phone.app_instance:
        bitrix_tasks.call_api.delay(phone.app_instance.id, "voximplant.sip.delete", {"CONFIG_ID": phone.voximplant_id})
        phone.voximplant_id = None

@login_required
def phone_details(request, phone_id):
    phone = get_object_or_404(Phone, id=phone_id, owner=request.user)
    templates = Template.objects.filter(waba=phone.waba, status='APPROVED')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'send_message':
            template = request.POST.get('template')
            recipient_phones_raw = request.POST.get('recipient_phone')
            recipients = [p.strip() for p in recipient_phones_raw.strip().splitlines() if p.strip()]
            waba_tasks.send_message.delay(template, recipients, phone_id)
            messages.success(request, _('The mailing has been added to the queue.'))
            return redirect('waba')
        elif action == 'update_calling':
            call_dest = request.POST.get('call_dest')
            save_required = False

            if call_dest == "disabled":
                phone.calling = "disabled"
                phone.sip_status = "disabled"
                save_required = True
            else:
                phone.calling = "enabled"
                phone.sip_status = "enabled"
            if call_dest != "b24":
                delete_voximplant(phone)
                save_required = True
            if call_dest == "pbx":
                phone.sip_hostname = request.POST.get('sip_hostname')
                phone.sip_port = request.POST.get('sip_port')
                save_required = True
            else:  # для всех, кроме pbx
                app = App.objects.get(id=WABA_APP_ID)
                if not app.sip_server:
                    messages.error(request, _("FreePBX Server not connected"))
                    return redirect('phone-details', phone_id=phone.id)
                phone.sip_hostname = app.sip_server.domain
                phone.sip_port = app.sip_server.sip_port
                save_required = True

            if phone.call_dest != call_dest:
                phone.call_dest = call_dest
                save_required = True

            if save_required:
                with transaction.atomic():
                    phone.save()
                    def after_commit():
                        if call_dest == "b24":
                            if not phone.app_instance:
                                text = _("Bitrix App not installed.")
                                msg = Message.objects.filter(code="wa_calling_b24_error").first()
                                if msg:
                                    text = msg.message
                                messages.error(request, text)
                                return
                            if phone.voximplant_id:
                                messages.info(request, _("This number is already connected."))
                                return
                            if not phone.sip_extensions:
                                ext = create_extension_task(phone.id)
                                phone.sip_extensions = ext
                                phone.save()
                            else:
                                ext = phone.sip_extensions
                            payload = {
                                "TITLE": f"{phone.phone} WhatsApp Cloud",
                                "SERVER": ext.server.domain,
                                "LOGIN": ext.number,
                                "PASSWORD": ext.password
                            }
                            try:
                                resp = bitrix_tasks.call_api(phone.app_instance.id, "voximplant.sip.add", payload)
                                result = resp.get("result", {})
                                voximplant_id = result.get("ID")
                                phone.voximplant_id = int(voximplant_id)
                                phone.save()
                            except Exception as e:
                                messages.error(request, e)
                                return

                        elif call_dest == "ext":
                            phone.refresh_from_db()
                            if not phone.sip_extensions:
                                ext = create_extension_task(phone.id)
                                phone.sip_extensions = ext
                                phone.save()
                            if not phone.sip_extensions:
                                messages.error(request, _("SIP extension creation failed."))                    
                        try:
                            waba_tasks.call_management(phone.id)
                            if call_dest == "disabled":
                                messages.info(request, _("Voice calls feature is disabled"))
                            else:
                                messages.success(request, _("Call destination %(dest)s enabled") % {'dest': call_dest})
                        except Exception as e:
                            phone.calling = "disabled"
                            phone.call_dest = "disabled"
                            phone.sip_user_password = ""
                            phone.save()
                            messages.error(request, str(e))
                            return 
                    transaction.on_commit(after_commit)
            return redirect('phone-details', phone_id=phone.id)
    
    if phone.date_end and timezone.now() > phone.date_end:
        messages.error(request, _('The tariff has expired ') + str(phone.date_end))
        return redirect("waba")
    return render(request, 'waba/phone.html', {'phone': phone, 'templates': templates})


@login_required
def manual_add(request):
    app = App.objects.get(id=WABA_APP_ID)
    if request.method == 'POST':
        waba_id = request.POST.get('waba_id')
        phone = request.POST.get('phone')
        phone_id = request.POST.get('phone_id')
        access_token = request.POST.get('access_token')

        headers = {"Authorization": f"Bearer {access_token}"}
        phone_response = requests.get(f"{API_URL}/v{app.api_version}.0/{phone_id}", headers=headers)
        phone_data = phone_response.json()
        if phone_response.status_code != 200:
            error = phone_data.get('error', {}).get('message')
            messages.error(request, error)
        else:
            phone = phone_data.get('display_phone_number')

            # Проверка на существование WABA
            waba, created = Waba.objects.get_or_create(
                waba_id=waba_id,
                defaults={'access_token': access_token, 'owner': request.user, 'app': app}
            )

            waba_utils.sample_template(access_token, waba_id)

            if created:
                waba.access_token = access_token
                waba.app = app
                waba.save()

            # Проверка на существование Phone
            try:
                phone_obj = Phone.objects.get(Q(phone_id=phone_id) | Q(phone=phone))
                messages.error(request, f'Phone {phone_obj.phone} is already registered!')
            except Phone.DoesNotExist:
                phone_obj = Phone.objects.create(
                    phone_id=phone_id,
                    phone=phone,
                    waba=waba,
                    owner=request.user
                )
                messages.success(request, f'Phone {phone_obj.phone} successfully added!')

        return redirect('waba')

    return render(request, 'manual-add.html')


@login_required
def save_request(request):

    user_id = request.user.id
    request_id = request.GET.get('request-id')

    if user_id and request_id:
        app = App.objects.get(id=WABA_APP_ID)
        redis_client.json().set(request_id, "$", {'user': user_id})
        redis_client.expire(request_id, 3600)
        params = {
            'client_id': app.client_id,
            'config_id': app.config_id,
            'response_type': 'code',
            'override_default_response_type': 'true',
            'redirect_uri': f'https://{app.site}/waba/callback/',
            'state': request_id,
        }
        url = f'https://www.facebook.com/v{app.api_version}.0/dialog/oauth?{urlencode(params)}'
        return redirect(url)
    else:
        return HttpResponseServerError({'error'})


@login_message_required(code="waba")
def waba_view(request):
    connector_service = "waba"
    phones = Phone.objects.filter(owner=request.user)
    instances = bitrix_utils.get_instances(request, connector_service)
    waba_lines = Line.objects.filter(owner=request.user, connector__service=connector_service)
    request_id = str(uuid.uuid4())
    if not instances:
        user_message(request, "install_waba")
    if request.method == "POST":
        days = request.POST.get('days')
        if days:
            request.session['waba_days'] = days
        else:
            phone_id = request.POST.get("phone_id")
            line_id = request.POST.get("line_id")
            phone = get_object_or_404(Phone, id=phone_id, owner=request.user)
            try:
                bitrix_utils.connect_line(request, line_id, phone, connector_service)
            except Exception as e:
                messages.error(request, str(e))
    else:
        days = request.session.get('waba_days', 7)

    try:
        days = int(request.session.get('waba_days', 7))
    except Exception:
        days = 7

    expire_notif_dt = timezone.now() + timezone.timedelta(days=days)
    for phone in phones:
        if getattr(phone, "date_end", None) and phone.date_end <= expire_notif_dt:
            phone.expiring_soon = True
        else:
            phone.expiring_soon = False
    return render(request, "waba/list.html", {
        "phones": phones,
        "waba_lines": waba_lines,
        "instances": instances,
        "request_id": request_id,
        "days": days,
    })


@csrf_exempt
def facebook_callback(request):

    app = App.objects.get(id=WABA_APP_ID)
    if not app:
        return HttpResponseServerError("App not found")
    
    if request.method == 'POST':
        data = json.loads(request.body)
        request_id = data.get('requestId')

        # Проверка на наличие request_id и данных в Redis
        if not request_id:
            return HttpResponseBadRequest("Request ID is missing")

        current_data = redis_client.json().get(request_id, "$")
        if not current_data:
            return HttpResponseBadRequest(f"No data found for request_id {request_id}")

        current_data = current_data[0]

        # Обработка sessionInfoResponse
        if 'sessionInfoResponse' in data:
            session_info_response = data.get('sessionInfoResponse', {})
            data_type = session_info_response.get('type')
            event = session_info_response.get('event')

            if data_type == 'WA_EMBEDDED_SIGNUP' and event == 'FINISH':
                new_data = session_info_response.get('data')
                if new_data:
                    current_data.update(new_data)
                    redis_client.json().set(request_id, "$", current_data)

            return HttpResponse("information saved")

        # Обработка partner_code
        elif 'partner_code' in data:
            partner_code = data.get('partner_code')
            current_data.update({'code': partner_code})
            redis_client.json().set(request_id, "$", current_data)

            payload = {
                'client_id': app.client_id,
                'client_secret': app.client_secret,
                'redirect_uri': f'https://{app.site}/waba/callback/',
                'code': current_data.get('code'),
            }

            url = f"{API_URL}/v{app.api_version}.0/oauth/access_token"
            response = requests.post(url, json=payload)
            if response.status_code != 200:
                return HttpResponseServerError("Failed to get access token")

            access_token = response.json().get('access_token')

            redis_client.json().set(request_id, "$.access_token", access_token)
            
            user = User.objects.get(id=current_data.get('user'))

            waba_id = current_data.get('waba_id')
            if not waba_id:
                return HttpResponse("WABA not found")
            
            waba, created = Waba.objects.get_or_create(
                waba_id=waba_id,
                app=app
            )

            waba.access_token = access_token
            waba.owner = user
            waba.save()

            phone_id = current_data.get('phone_number_id')
            phone, created = Phone.objects.get_or_create(phone_id=phone_id)
            phone.waba = waba
            phone.owner = user

            headers = {"Authorization": f"Bearer {access_token}"}
            phone_data = requests.get(f"{API_URL}/v{app.api_version}.0/{phone_id}", headers=headers)
            if phone_data.status_code != 200:
                return HttpResponseServerError("Failed to retrieve phone data")

            phone_data = phone_data.json()
            phone.phone = phone_data.get('display_phone_number')
            phone.save()

            payload = {
                'messaging_product': 'whatsapp',
                'pin': '000000'
            }
            url = f"{API_URL}/v{app.api_version}.0/{phone_id}/register"
            requests.post(url, json=payload, headers=headers)
            url = f"{API_URL}/v{app.api_version}.0/{waba_id}/subscribed_apps"
            requests.post(url, json=payload, headers=headers)

            waba_utils.sample_template(access_token, waba_id)
            
            return HttpResponse('Request Accepted', status=202)
            

        return HttpResponse("Success: Phone and WABA information saved")

            # return HttpResponseBadRequest("Missing required data: 'code' or 'phone_number_id'")

    elif request.method == 'GET':

        error = request.GET.get('error')
        if error:
            params = request.GET.copy()
            params.pop('state', None)
            result = dict(params)
            json_result = json.dumps(result)
            messages.error(request, json_result)
            return redirect('waba')
        
        code = request.GET.get('code')
        request_id = request.GET.get('state')
        if not request_id:
            messages.error(request, "Request ID is missing")
            return redirect('waba')
        current_data = redis_client.json().get(request_id, "$")
        if not current_data:
            messages.error(request, "Request data is missing")
            return redirect('waba')
        
        waba_tasks.add_waba_phone.delay(current_data, code, request_id)

        messages.success(request, 'Номер успешно добавлен. Через пару минут он отобразиться здесь.')

        return redirect('waba')
    
    else:
        return HttpResponseBadRequest("Invalid request method")

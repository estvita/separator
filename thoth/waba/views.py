from django.shortcuts import render, redirect, get_object_or_404

from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.db.models import Q

import redis
import requests
import json
import uuid
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponseBadRequest, HttpResponseServerError, HttpResponse

from django.utils import timezone

from urllib.parse import urlencode

import thoth.bitrix.utils as bitrix_utils

import logging

from thoth.users.models import User, Message
from thoth.bitrix.models import AppInstance, Line, Connector

from django.conf import settings

from .models import App, Waba, Phone, Template
import thoth.waba.utils as utils

import thoth.waba.tasks as wa_tasks

API_URL = 'https://graph.facebook.com/'

logger = logging.getLogger(__name__)
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

WABA_APP_ID = settings.WABA_APP_ID


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


@login_required
def phone_details(request, phone_id):
    phone = get_object_or_404(Phone, id=phone_id, owner=request.user)
    templates = Template.objects.filter(waba=phone.waba, status='APPROVED')

    if request.method == 'POST':
        template = request.POST.get('template')
        recipient_phones_raw = request.POST.get('recipient_phone')
        recipients = [phone.strip() for phone in recipient_phones_raw.strip().splitlines() if phone.strip()]
        wa_tasks.send_message.delay(template, recipients, phone_id)

        messages.success(request, f'Рассылка добавлена в очередь. Продолжить общение с клинетами можете в чате.')

        return redirect('waba')

    if timezone.now() > phone.date_end:
        messages.error(request, f'Срок дествия вашего тарифа истек {phone.date_end}')
    return render(request, 'phone_details.html', {'phone': phone, 'templates': templates})


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
                defaults={'access_token': access_token, 'owner': request.user}
            )

            utils.sample_template(access_token, waba_id)

            if created:
                waba.access_token = access_token
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


@login_required
def waba_view(request):
    connector_service = "waba"
    connector = Connector.objects.filter(service=connector_service).first()
    phones = Phone.objects.filter(owner=request.user)
    instances = AppInstance.objects.filter(owner=request.user, app__connectors=connector)
    waba_lines = Line.objects.filter(connector=connector, owner=request.user)
    request_id = str(uuid.uuid4())

    if request.method == "POST":
        phone_id = request.POST.get("phone_id")
        line_id = request.POST.get("line_id")

        phone = get_object_or_404(Phone, id=phone_id, owner=request.user)

        bitrix_utils.connect_line(request, line_id, phone, connector, "waba")

    message = Message.objects.filter(code="waba").first()
    
    return render(request, "waba.html", {
        "phones": phones,
        "waba_lines": waba_lines,
        "instances": instances,
        "request_id": request_id,
        "message": message,
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
                waba_id=waba_id)

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

            utils.sample_template(access_token, waba_id)
            
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
        
        wa_tasks.add_waba_phone.delay(current_data, code, request_id)

        messages.success(request, 'Номер успешно добавлен. Через пару минут он отобразиться здесь.')

        return redirect('waba')
    
    else:
        return HttpResponseBadRequest("Invalid request method")

import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import WaSession, WaServer
from django.contrib import messages
from django.conf import settings
from .forms import SendMessageForm
from django.utils import timezone
from thoth.tariff.utils import get_trial
from thoth.bitrix.models import AppInstance, Line, Connector
import thoth.bitrix.utils as bitrix_utils

from .tasks import send_message_task

WABWEB_SRV = settings.WABWEB_SRV


@login_required
def wa_sessions(request):
    connector_service = "waweb"
    connector = Connector.objects.filter(service=connector_service).first()
    if request.method == "POST":
        session_id = request.POST.get("session_id")
        line_id = request.POST.get("line_id")
        phone = get_object_or_404(WaSession, id=session_id, owner=request.user)
        bitrix_utils.connect_line(request, line_id, phone, connector, connector_service)
        return redirect('waweb')

    sessions = WaSession.objects.filter(owner=request.user)
    instances = AppInstance.objects.filter(owner=request.user, app__connectors=connector)
    wa_lines = Line.objects.filter(connector=connector, owner=request.user)

    # Проверка наличия активных сессий
    for session in sessions:
        session.show_link = session.status == "open"

    return render(
        request, 'waweb/wa_sessions.html', {
            "sessions": sessions,
            "instances": instances,
            "wa_lines": wa_lines,
        }
    )


@login_required
def connect_number(request, session_id=None):
    if not session_id:
        # проверка наличия неподключенных сессий
        sessions = WaSession.objects.filter(
            phone__isnull=True,
            owner=request.user
        )
        if sessions:
            messages.warning(request, "У вас уже есть незавершенное подключение. Нажмите 'Подключить'")
            return redirect('waweb')
        # Создаем новую сессию
        new_session = WaSession.objects.create(owner=request.user)
        session_id = new_session.session

        # период
        if not new_session.date_end:
            new_session.date_end = get_trial(request.user, "waweb")
        new_session.save()

    wa_server = WaServer.objects.get(id=WABWEB_SRV)
    headers = {"apikey": wa_server.api_key}
    # Отправляем запрос на старт сессии
    payload = {
        "instanceName": str(session_id),
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "alwaysOnline": wa_server.always_online,
        "groupsIgnore": wa_server.groups_ignore,
        "readMessages": wa_server.read_messages,
    }
    response = requests.post(f"{wa_server.url}instance/create", json=payload, headers=headers)

    if response.status_code == 201:
        inst_data = response.json()
        instanceId = inst_data.get("instance", {}).get("instanceId")
        new_session.instanceId = instanceId
        new_session.save()
        img_data = inst_data.get("qrcode", {}).get("base64", "")
        if img_data:
            img_data = img_data.split(",", 1)[1]
            request.session['qr_image'] = img_data
            return redirect('qr_code_page', session_id=session_id)
        else:
            url = f"{wa_server.url}instance/delete/{session_id}"
            del_data = requests.delete(url, headers=headers)
            new_session.delete()
            messages.error(request, "Failed to initiate session.")
    else:
        url = f"{wa_server.url}instance/delete/{session_id}"
        del_data = requests.delete(url, headers=headers)
        new_session.delete()
        messages.error(request, "Failed to initiate session.")
    return redirect('waweb')


@login_required
def qr_code_page(request, session_id):
    qr_image = request.session.pop('qr_image', '')
    if not qr_image:
        wa_server = WaServer.objects.get(id=WABWEB_SRV)
        gr_url = f"{wa_server.url}instance/connect/{session_id}"
        headers = {"apikey": wa_server.api_key}
        response = requests.get(gr_url, headers=headers)
        if response.status_code == 200:
            inst_data = response.json()
            img_data = inst_data.get("base64", "")
            if img_data:
                qr_image = img_data.split(",", 1)[1]
            else:
                messages.error(request, "Failed to restart session.")
                return redirect('waweb')
        else:
            messages.error(request, "Failed to restart session.")
            return redirect('waweb')
    return render(request, 'waweb/qr_code.html', {
        'session_id': session_id,
        'qr_image': qr_image,
    })



@login_required
def send_message_view(request, session_id):
    session = get_object_or_404(WaSession, session=session_id, owner=request.user)

    if timezone.now() > session.date_end:
        messages.error(request, f'Срок дествия вашего тарифа истек {session.date_end}')
        return redirect('waweb')

    if request.method == "POST":
        if session.status == 'close':
            messages.error(request, "Телефон не подключен. Необходимо произвести повторное подключение.")
            return redirect('waweb')
        form = SendMessageForm(request.POST)
        if form.is_valid():
            recipients_raw = form.cleaned_data['recipients']
            message = form.cleaned_data['message']
            recipients = [line.strip() for line in recipients_raw.splitlines() if line.strip()]
            
            send_message_task.delay(str(session.session), recipients, message, "string", True)
            
            messages.success(request, "Задача на отправку сообщений создана.")
            return redirect('waweb')
    else:
        form = SendMessageForm()

    return render(request, 'waweb/send_message.html', {
        'form': form,
        'session': session,
    })
    
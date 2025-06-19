import requests
import redis
import uuid
from requests.exceptions import RequestException
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Session, Server
from django.contrib import messages
from django.conf import settings
from django.db.models import Count
from .forms import SendMessageForm
from django.utils import timezone
from thoth.tariff.utils import get_trial
from thoth.bitrix.models import AppInstance, Line, Connector
import thoth.bitrix.utils as bitrix_utils

from .tasks import send_message_task

WA_SESSIONS_PER_SERVER = settings.WA_SESSIONS_PER_SERVER

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

LINK_TTL = 60 * 60 * 24

@login_required
def wa_sessions(request):
    connector_service = "waweb"
    connector = Connector.objects.filter(service=connector_service).first()
    if request.method == "POST":
        session_id = request.POST.get("session_id")
        line_id = request.POST.get("line_id")
        if not line_id:
            messages.warning(request, "Необходимо выбрать линию из списка или создать новую.")
            return redirect('waweb')
        phone = get_object_or_404(Session, id=session_id, owner=request.user)
        if not phone.phone:
            messages.error(request, "Сначала необходимо подключить WhatsApp.")
            return redirect('waweb')
        if phone.line and str(phone.line.id) == str(line_id):
            messages.warning(request, "Эта линия уже подключена к выбранной сессии.")
            return redirect('waweb')
        try:
            bitrix_utils.connect_line(request, line_id, phone, connector, connector_service)
        except Exception as e:
            messages.error(request, str(e))
            return redirect('waweb')
        return redirect('waweb')

    sessions = Session.objects.filter(owner=request.user)
    instances = AppInstance.objects.filter(owner=request.user, app__connectors=connector)
    wa_lines = Line.objects.filter(connector=connector, owner=request.user)

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
        sessions = Session.objects.filter(
            phone__isnull=True,
            owner=request.user
        )
        if sessions and not request.user.integrator:
            messages.warning(request, "У вас уже есть незавершенное подключение. Нажмите 'Подключить'")
            return redirect('waweb')
        
        new_session = Session.objects.create(owner=request.user)
        session_id = new_session.session

    if not new_session.date_end:
        new_session.date_end = get_trial(request.user, "waweb")
        # new_session.save()

    server = (
        Server.objects.annotate(connected_sessions=Count('sessions'))
        .filter(connected_sessions__lt=WA_SESSIONS_PER_SERVER)
        .order_by('id')
        .first()
    )

    if not server:
        messages.error(request, "Нет доступных серверов.")
        new_session.delete()
        return redirect('waweb')

    new_session.server = server
    new_session.save()

    headers = {"apikey": server.api_key}
    payload = {
        "instanceName": str(session_id),
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "alwaysOnline": server.always_online,
        "groupsIgnore": server.groups_ignore,
        "readMessages": server.read_messages,
    }

    try:
        response = requests.post(f"{server.url}instance/create", json=payload, headers=headers, timeout=15)
        inst_data = response.json()
        instanceId = inst_data.get("instance", {}).get("instanceId")
        new_session.instanceId = instanceId
        new_session.save()
        img_data = inst_data.get("qrcode", {}).get("base64", "")
        if img_data:
            img_data = img_data.split(",", 1)[1]
            request.session['qr_image'] = img_data
        return redirect('qr_code_page', session_id=session_id)
    except RequestException as e:
        print("request error:", e)
        new_session.delete()
        messages.error(request, "Failed to initiate session.")
        return redirect('waweb')


def get_gr(request, session):

    server = session.server
    if not server:
        messages.error(request, "Session is not attached to a server.")
        return
    
    if session.status == "open":
        messages.warning(request, "Session is connected.")
        return

    gr_url = f"{server.url}instance/connect/{session.session}"
    headers = {"apikey": server.api_key}
    try:
        response = requests.get(gr_url, headers=headers)
        inst_data = response.json()
        img_data = inst_data.get("base64", "")
        if img_data:
            qr_image = img_data.split(",", 1)[1]
            return qr_image
        else:
            messages.error(request, "Failed to restart session.")
            return
    except RequestException:
        messages.error(request, "Failed connect to server")
        return


@login_required
def qr_code_page(request, session_id):
    qr_image = request.session.pop('qr_image', '')
    try:
        session = Session.objects.get(session=session_id)
    except Session.DoesNotExist:
        messages.error(request, "Session not found.")
        return redirect('waweb')
    
    if not qr_image:
        qr_image = get_gr(request, session)

    if not qr_image:
        return redirect('waweb')

    public_id = redis_client.get(f"public_qr:{session_id}")
    if not public_id:
        public_id = str(uuid.uuid4())
        redis_client.set(f"public_qr:{session_id}", public_id, ex=LINK_TTL)
        redis_client.set(f"public_qr:{public_id}", str(session_id), ex=LINK_TTL)    

    return render(request, 'waweb/qr_code.html', {
        'qr_image': qr_image,
        'request': request,
        'public_id': public_id,
    })


def share_qr(request, public_id):
    session_id = redis_client.get(f"public_qr:{public_id}")
    if not session_id:
        messages.error(request, "Временная ссылка истекла или некорректна.")
        return redirect('waweb')
    try:
        session = Session.objects.get(session=session_id)
    except Session.DoesNotExist:
        messages.error(request, "Session not found.")
        return redirect('waweb')
    qr_image = get_gr(request, session)
    if not qr_image:
        return redirect('waweb')
    return render(request, 'waweb/qr_code.html', {
        'qr_image': qr_image,
        'request': request,
        'public_id': public_id,
    })


@login_required
def send_message_view(request, session_id):
    session = get_object_or_404(Session, session=session_id, owner=request.user)

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
    
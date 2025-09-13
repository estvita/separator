import requests
import redis
import uuid
from requests.exceptions import RequestException
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count, Q, F
from django.utils import timezone
from django.conf import settings

from thoth.bitrix.models import Line, Bitrix
import thoth.bitrix.utils as bitrix_utils

from thoth.users.models import Message
from thoth.decorators import login_message_required, user_message

from .models import Session, Server
from .forms import SendMessageForm
from .tasks import send_message_task

apps = settings.INSTALLED_APPS

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

LINK_TTL = 60 * 60 * 24

@login_message_required(code="waweb")
def wa_sessions(request):
    connector_service = "waweb"
    sessions = Session.objects.filter(owner=request.user)
    wa_lines = Line.objects.filter(owner=request.user, connector__service=connector_service)
    instances = bitrix_utils.get_instances(request, connector_service)
    if not instances:
        user_message(request, "install_waweb")
    portals = Bitrix.objects.filter(owner=request.user)
    selected_portal = None

    if request.method == "POST":
        if "filter_portal_id" in request.POST:
            filter_portal_id = request.POST.get("filter_portal_id")
            if filter_portal_id:
                if filter_portal_id == 'all':
                    request.session.pop('b24_data', None)
                else:
                    selected_portal = portals.filter(id=filter_portal_id).first()
                    if selected_portal:
                        request.session['b24_data'] = {"member_id": selected_portal.member_id}
            return redirect('waweb')        

        # days поле
        days = request.POST.get('days')
        if days:
            request.session['waweb_days'] = days
        else:
            session_id = request.POST.get("session_id")
            line_id = request.POST.get("line_id")
            phone = get_object_or_404(Session, id=session_id, owner=request.user)
            if not phone.phone:
                messages.error(request, "Сначала необходимо подключить WhatsApp.")
                return redirect('waweb')
            try:
                bitrix_utils.connect_line(request, line_id, phone, connector_service)
            except Exception as e:
                messages.error(request, str(e))
    else:
        days = request.session.get('waweb_days', 7)
    
    try:
        days = int(request.session.get('waweb_days', 7))
    except Exception:
        days = 7

    expire_notif_dt = timezone.now() + timezone.timedelta(days=days)

    for session in sessions:
        session.show_link = session.status == "open"
        if session.date_end and session.date_end <= expire_notif_dt:
            session.expiring_soon = True
        else:
            session.expiring_soon = False

    return render(
        request, 'waweb/wa_sessions.html', {
            "sessions": sessions,
            "instances": instances,
            "wa_lines": wa_lines,
            "days": days,
            "portals": portals,
            "selected_portal_id": request.session.get('b24_data', {}).get('member_id') if request.session.get('b24_data') else "all",
        }
    )


def create_instance(session):
    server = session.server
    headers = {"apikey": server.api_key}
    
    payload = {
        "instanceName": str(session.session),
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
        session.instanceId = instanceId
        session.save()
        img_data = inst_data.get("qrcode", {}).get("base64", "")
        img_data = img_data.split(",", 1)[1]
        return img_data
    except RequestException as e:
        print("request error:", e)
        return None


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

    else:
        new_session = get_object_or_404(Session, session=session_id, owner=request.user)

    if "thoth.tariff" in apps and not new_session.date_end:
        from thoth.tariff.utils import get_trial
        new_session.date_end = get_trial(request.user, "waweb")
        # new_session.save()

    server = (
        Server.objects.annotate(
            connected_sessions=Count('sessions', filter=Q(sessions__status='open'))
        )
        .filter(connected_sessions__lt=F('max_connections'))
        .order_by('id')
        .first()
    )

    if not server:
        messages.error(request, "Нет доступных серверов.")
        new_session.delete()
        return redirect('waweb')

    new_session.server = server
    new_session.save()

    try:
        img_data = create_instance(new_session)
        if img_data:
            request.session['qr_image'] = img_data       
        return redirect('qr_code_page', session_id=session_id)
    except Exception as e:
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
        if response.status_code == 404:
            return create_instance(session)
        
        inst_data = response.json()
        img_data = inst_data.get("base64", "")
        if img_data:
            qr_image = img_data.split(",", 1)[1]
            return qr_image
        else:
            messages.error(request, f"Failed to restart session. {inst_data}")
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

    message = Message.objects.filter(code="waweb_instruction").first()
    if message:
        messages.info(request, message.message)
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
    message = Message.objects.filter(code="waweb_instruction").first()
    if message:
        messages.info(request, message.message)
    return render(request, 'waweb/qr_code.html', {
        'qr_image': qr_image,
        'request': request,
        'public_id': public_id,
    })


@login_required
def send_message_view(request, session_id):
    session = get_object_or_404(Session, session=session_id, owner=request.user)

    if session.date_end and timezone.now() > session.date_end:
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
            
            send_message_task.delay(str(session.session), recipients, message)
            
            messages.success(request, "Задача на отправку сообщений создана.")
            return redirect('waweb')
    else:
        form = SendMessageForm()

    return render(request, 'waweb/send_message.html', {
        'form': form,
        'session': session,
    })
    

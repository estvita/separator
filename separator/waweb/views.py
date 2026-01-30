import requests
import redis
import uuid
import base64
from requests.exceptions import RequestException
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count, Q, F
from django.utils import timezone
from django.conf import settings
from django.utils.translation import gettext as _
import separator.bitrix.utils as bitrix_utils

from separator.decorators import login_message_required, user_message

from .models import Session, Server
from .forms import SendMessageForm
from .tasks import send_message

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL, decode_responses=True)

LINK_TTL = 60 * 60 * 24

@login_message_required(code="waweb")
def wa_sessions(request):
    connector_service = "waweb"
    portals, instances, lines = bitrix_utils.get_instances(request, connector_service)
    b24_data = request.session.get('b24_data')
    selected_portal = None
    if b24_data:
        member_id = b24_data.get("member_id")
        if member_id:
            selected_portal = portals.filter(member_id=member_id).first()
    if selected_portal:
        sessions = Session.objects.filter(
            Q(line__portal=selected_portal) | Q(owner=request.user, line__isnull=True)
        )
        lines = lines.filter(portal=selected_portal)
        instances = instances.filter(portal=selected_portal)
    else:
        sessions = Session.objects.filter(
            Q(line__portal__in=portals) | Q(owner=request.user)
        )

    if not instances:
        user_message(request, "waweb_install")
    
    if request.method == "POST":
        # days поле
        days = request.POST.get('days')
        if days:
            request.session['waweb_days'] = days
        if "filter_portal_id" in request.POST:
            filter_portal_id = request.POST.get("filter_portal_id")
            if filter_portal_id == "all":
                request.session.pop('b24_data', None)
            else:
                portal = portals.filter(id=filter_portal_id).first()
                if portal:
                    request.session['b24_data'] = {"member_id": portal.member_id}
            return redirect('waweb')
        session_id = request.POST.get("session_id")
        line_id = request.POST.get("line_id")
        phone = get_object_or_404(Session, id=session_id)
        if not phone.phone:
            messages.error(request, _("You must connect WhatsApp first."))
            return redirect('waweb')
        try:
            bitrix_utils.connect_line(request, line_id, phone, connector_service)
        except Exception as e:
            messages.error(request, str(e))

    days = request.session.get('waweb_days', 7)
    try:
        days = int(days)
    except Exception:
        days = 7
    expire_notif_dt = timezone.now() + timezone.timedelta(days=days)

    for session in sessions:
        session.show_link = session.status == "open"
        session.expiring_soon = session.date_end and session.date_end <= expire_notif_dt

    return render(
        request, 'waweb/wa_sessions.html', {
            "sessions": sessions,
            "instances": instances,
            "wa_lines": lines,
            "days": days,
            "portals": portals,
            "selected_portal_id": selected_portal.id if selected_portal else "all",
        }
    )


def get_available_server():
    return (
        Server.objects.annotate(
            connected_sessions=Count('sessions', filter=Q(sessions__status='open'))
        )
        .filter(connected_sessions__lt=F('max_connections'))
        .order_by('id')
        .first()
    )


def create_instance(session):
    if not session.server:
        session.server = get_available_server()
        session.save()
        
    server = session.server
    if not server:
        return None

    headers = {"apikey": server.api_key}
    
    payload = {
        "instanceName": str(session.session),
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "alwaysOnline": server.always_online,
        "groupsIgnore": server.groups_ignore,
        "readMessages": server.read_messages,
        "syncFullHistory": server.sync_history,
    }

    try:
        response = requests.post(f"{server.url}/instance/create", json=payload, headers=headers, timeout=15)
        inst_data = response.json()
        instanceId = inst_data.get("instance", {}).get("instanceId")
        session.instanceId = instanceId
        session.save()
        img_data = inst_data.get("qrcode", {}).get("base64", "")
        if img_data and "," in img_data:
            img_data = img_data.split(",", 1)[1]
        return img_data
    except (RequestException, Exception) as e:
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
            messages.warning(request, _("You already have an incomplete connection. Click 'Connect'"))
            return redirect('waweb')
        
        new_session = Session.objects.create(owner=request.user)
        session_id = new_session.session

    else:
        new_session = get_object_or_404(Session, session=session_id, owner=request.user)

    server = get_available_server()

    if not server:
        messages.error(request, _("No available servers."))
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

    if not session.server:
        session.server = get_available_server()
        session.save()

    server = session.server
    if not server:
        messages.error(request, _("No available servers."))
        return
    
    if session.status == "open":
        messages.warning(request, "Session is connected.")
        return

    gr_url = f"{server.url}/instance/connect/{session.session}"
    headers = {"apikey": server.api_key}
    try:
        response = requests.get(gr_url, headers=headers)
        if response.status_code == 404:
            return create_instance(session)
        
        inst_data = response.json()
        img_data = inst_data.get("base64", "")
        if img_data and "," in img_data:
            qr_image = img_data.split(",", 1)[1]
            return qr_image
        elif img_data:
             return img_data
        else:
            messages.error(request, f"Failed to restart session. {inst_data}")
            return
    except (RequestException, Exception):
        messages.error(request, "Failed connect to server")
        return


@login_required
def qr_code_page(request, session_id):
    qr_image = request.session.pop('qr_image', '')
    try:
        session = Session.objects.get(session=session_id, owner=request.user)
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

    user_message(request, "waweb_instruction")
    return render(request, 'waweb/qr_code.html', {
        'qr_image': qr_image,
        'request': request,
        'public_id': public_id,
    })


def share_qr(request, public_id):
    session_id = redis_client.get(f"public_qr:{public_id}")
    if not session_id:
        messages.error(request, _("The temporary link has expired or is invalid."))
        return redirect('waweb')
    try:
        session = Session.objects.get(session=session_id)
    except Session.DoesNotExist:
        messages.error(request, "Session not found.")
        return redirect('waweb')
    qr_image = get_gr(request, session)
    if not qr_image:
        return redirect('waweb')
    user_message(request, "waweb_instruction")
    return render(request, 'waweb/qr_code.html', {
        'qr_image': qr_image,
        'request': request,
        'public_id': public_id,
    })


@login_required
def send_message_view(request, session_id):
    session = get_object_or_404(Session, session=session_id, owner=request.user)

    if session.date_end and timezone.now() > session.date_end:
        messages.error(request, _('Your tariff expired on %(date)s') % {'date': session.date_end})
        return redirect('waweb')

    if request.method == "POST":
        if session.status == 'close':
            messages.error(request, _("Phone not connected. Reconnection required."))
            return redirect('waweb')
        form = SendMessageForm(request.POST, request.FILES)
        if form.is_valid():
            recipients_raw = form.cleaned_data['recipients']
            message = form.cleaned_data.get('message')
            file_obj = form.cleaned_data.get('file')
            recipients = [line.strip() for line in recipients_raw.splitlines() if line.strip()]

            file_data = None
            if file_obj:
                try:
                    file_content = file_obj.read()
                    encoded = base64.b64encode(file_content).decode("utf-8")
                    file_data = {
                        "mimetype": file_obj.content_type,
                        "data": encoded,
                        "filename": file_obj.name
                    }
                except Exception as e:
                    messages.error(request, f"Error processing file: {e}")
                    return redirect('waweb')

            for recipient in recipients:
                if file_data:
                    send_message.delay(session.session, recipient, file_data, cont_type="media", caption=message)
                elif message:
                    send_message.delay(session.session, recipient, message)
            
            messages.success(request, _("Message sending task created."))
            return redirect('waweb')
    else:
        form = SendMessageForm()

    return render(request, 'waweb/send_message.html', {
        'form': form,
        'session': session,
    })
    

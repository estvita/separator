import uuid
import json
import redis
import logging
from django.shortcuts import render, redirect, get_object_or_404

from django.contrib import messages
from django.db import transaction
from django.db.models import Q

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseServerError
from django.utils import timezone
from django.utils.translation import gettext as _

from urllib.parse import urlencode
from separator.decorators import login_message_required, user_message

import separator.bitrix.utils as bitrix_utils
import separator.bitrix.tasks as bitrix_tasks

from .models import App, Waba, Phone, Template
import separator.waba.utils as waba_utils
import separator.waba.tasks as waba_tasks

from separator.freepbx.tasks import create_extension_task

logger = logging.getLogger(__name__)
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


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
                app = waba_utils.get_app()
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
                                user_message(request, "waba_calling_error", "error")
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
                            waba_tasks.call_management.delay(phone.id)
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


@login_message_required(code="waba")
def waba_view(request):
    connector_service = "waba"
    portals, instances, lines = bitrix_utils.get_instances(request, connector_service)
    request_id = str(uuid.uuid4())
    if not instances:
        user_message(request, "waba_install")
    b24_data = request.session.get('b24_data')
    selected_portal = None
    if b24_data:
        member_id = b24_data.get("member_id")
        if member_id:
            selected_portal = portals.filter(member_id=member_id).first()
    if selected_portal:
        phones = Phone.objects.filter(
            Q(line__portal=selected_portal) | Q(owner=request.user, line__isnull=True)
        )
        lines = lines.filter(portal=selected_portal)
        instances = instances.filter(portal=selected_portal)
    else:
        phones = Phone.objects.filter(
            Q(line__portal__in=portals) | Q(owner=request.user)
        )

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
            return redirect('waba')  
        days = request.POST.get('days')
        if days:
            request.session['waba_days'] = days
        else:
            phone_id = request.POST.get("phone_id")
            line_id = request.POST.get("line_id")
            phone = get_object_or_404(Phone, id=phone_id)
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
        "waba_lines": lines,
        "instances": instances,
        "request_id": request_id,
        "days": days,
        "portals": portals,
        "selected_portal_id": request.session.get('b24_data', {}).get('member_id') if request.session.get('b24_data') else "all",
    })


@login_required
def save_request(request):
    user_id = request.user.id
    request_id = request.GET.get('request-id')

    if user_id and request_id:
        app = waba_utils.get_app()
        redis_client.json().set(request_id, "$", {'user': user_id})
        redis_client.expire(request_id, 7200)
        params = {
            'client_id': app.client_id,
            'config_id': app.config_id,
            'response_type': 'code',
            'redirect_uri': f'https://{app.site}/waba/callback/',
            'state': request_id,
        }
        url = f'https://www.facebook.com/v{app.api_version}.0/dialog/oauth?{urlencode(params)}'
        return redirect(url)
    else:
        return HttpResponseServerError({'error'})

# https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
@login_required
def facebook_callback(request):
    app = waba_utils.get_app()
    if not app:
        messages.error("App not found")
        return redirect('waba')

    if request.method == 'GET':
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
        existing = redis_client.json().get(request_id)
        if not existing:
            messages.error(request, "Request data is missing")
            return redirect('waba')
        redis_client.json().set(request_id, '$.code', code)        
        waba_tasks.add_waba_phone.delay(request_id)
        messages.success(request, 'Номер успешно добавлен. Через пару минут он отобразиться здесь.')
        return redirect('waba')
    
    else:
        return HttpResponseBadRequest("Invalid request method")

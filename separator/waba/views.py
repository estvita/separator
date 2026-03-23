import uuid
import json
import redis
import logging
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, OuterRef, Subquery, CharField
from django.core.paginator import Paginator

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseServerError
from django.utils import timezone
from django.utils.translation import gettext as _
from django.db.models.functions import Cast

from urllib.parse import urlencode
from separator.decorators import login_message_required, user_message

import separator.bitrix.utils as bitrix_utils
import separator.bitrix.tasks as bitrix_tasks

from .models import App, Waba, Phone, Template, TemplateBroadcast, TemplateBroadcastRecipient, CtwaEvents
import separator.waba.utils as waba_utils
import separator.waba.tasks as waba_tasks

from separator.freepbx.tasks import create_extension_task
from django.conf import settings

logger = logging.getLogger(__name__)
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)
WABA_STATUS_FIELDS = (
    "name,timezone_id,message_template_namespace,account_review_status,"
    "business_verification_status,country,ownership_type,primary_business_location"
)
PHONE_STATUS_FIELDS = (
    "display_phone_number,verified_name,status,quality_rating,country_code,"
    "country_dial_code,code_verification_status,account_mode,host_platform,"
    "messaging_limit_tier,is_official_business_account"
)


def delete_voximplant(phone):
    if phone.voximplant_id and phone.app_instance:
        bitrix_tasks.call_api.delay(phone.app_instance.id, "voximplant.sip.delete", {"CONFIG_ID": phone.voximplant_id})
        phone.voximplant_id = None

@login_required
def phone_details(request, phone_id):
    phone = get_object_or_404(Phone, phone_id=phone_id, owner=request.user)
    phone_status_json = ""
    phone_status_error = ""
    ctwa_query = request.GET.get("ctwa_q", "").strip()
    templates = Template.objects.filter(waba=phone.waba).prefetch_related(
        "components__named_params",
        "components__positional_params",
        "components__buttons",
        "components__buttons__named_params",
        "components__buttons__positional_params",
    )
    for template in templates:
        template.bitrix_code = waba_utils.build_bitrix_template_code(template)

    templates_data = []
    for template in templates:
        components_data = []
        for component in template.components.order_by("index", "id"):
            buttons_data = []
            for button in component.buttons.order_by("index", "id"):
                buttons_data.append({
                    "id": button.id,
                    "type": button.type,
                    "index": button.index,
                    "named_params": [
                        {"name": p.name} for p in button.named_params.order_by("id")
                    ],
                    "positional_params": [
                        {"position": p.position} for p in button.positional_params.order_by("position", "id")
                    ],
                })
            components_data.append({
                "id": component.id,
                "type": component.type,
                "format": component.format,
                "index": component.index,
                "text": component.text,
                "named_params": [
                    {"name": p.name} for p in component.named_params.order_by("id")
                ],
                "positional_params": [
                    {"position": p.position} for p in component.positional_params.order_by("position", "id")
                ],
                "buttons": buttons_data,
            })
        templates_data.append({
            "id": template.id,
            "label": f"{template.name} ({template.lang})",
            "lang": template.lang,
            "components": components_data,
        })

    latest_event_subquery = CtwaEvents.objects.filter(
        ctwa_id=OuterRef("pk")
    ).order_by("-date", "-id")
    ctwa_qs = phone.ctwas.annotate(
        id_text=Cast("id", output_field=CharField()),
        phone_text=Cast("phone", output_field=CharField()),
        source_id_text=Cast("source_id", output_field=CharField()),
        last_event=Subquery(latest_event_subquery.values("event")[:1]),
    ).order_by("-id")
    if ctwa_query:
        ctwa_qs = ctwa_qs.filter(
            Q(id_text__icontains=ctwa_query)
            | Q(phone_text__icontains=ctwa_query)
            | Q(source_id_text__icontains=ctwa_query)
        )
    ctwa_page_obj = Paginator(ctwa_qs, 50).get_page(request.GET.get("ctwa_page"))

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'send_message':
            template = request.POST.get('template')
            recipient_phones_raw = request.POST.get('recipient_phone')
            recipients = [p.strip() for p in recipient_phones_raw.strip().splitlines() if p.strip()]
            try:
                waba_tasks.send_message.delay(template, recipients, phone.id)
                messages.success(request, _('The mailing has been added to the queue.'))
                return redirect('waba')
            except Exception as e:
                messages.error(request, str(e))
                return redirect('phone-details', phone_id=phone.phone_id)
        elif action == 'update_templates':
            delete_ids = set(request.POST.getlist('delete_templates'))
            if delete_ids:
                to_delete_ids = list(templates.filter(id__in=delete_ids).values_list("id", flat=True))
                for template_id in to_delete_ids:
                    waba_tasks.delete_template.delay(template_id, request.user.id)
                if to_delete_ids:
                    messages.success(request, _("Template deletion queued: %(count)s") % {'count': len(to_delete_ids)})

            allowed_ids = set(request.POST.getlist('available_templates'))
            if delete_ids:
                allowed_ids -= delete_ids
            templates.update(availableInB24=False)
            if allowed_ids:
                templates.filter(id__in=allowed_ids).update(availableInB24=True)

            # Handle default template selection (only one per WABA)
            default_id = request.POST.get('default_template')
            if default_id and default_id not in delete_ids:
                templates.update(default=False)
                templates.filter(id=default_id).update(default=True)

            messages.success(request, _('Template availability updated.'))
            return redirect('phone-details', phone_id=phone.phone_id)
        elif action == 'update_calling':
            call_dest = request.POST.get('call_dest')
            save_required = False

            if call_dest == "disabled":
                phone.calling = "disabled"
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
                domain = request.get_host().split(':')[0]
                app = App.objects.filter(sites__domain__iexact=domain).first()
                if not app or not app.sip_server:
                    messages.error(request, _("FreePBX Server not connected"))
                    return redirect('phone-details', phone_id=phone.phone_id)
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
                                "TITLE": f"{phone.phone} WhatsApp",
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
            return redirect("waba")
        elif action == "check_phone_status":
            if not phone.waba or not phone.waba.app:
                phone_status_error = _("App is not connected to this phone WABA account.")
            else:
                try:
                    status_data = waba_utils.call_api(
                        waba=phone.waba,
                        endpoint=phone.phone_id,
                        payload={"fields": PHONE_STATUS_FIELDS},
                    )
                    phone_status_json = json.dumps(status_data, ensure_ascii=False, indent=2)
                except Exception as e:
                    phone_status_error = str(e)
    
    if phone.date_end and timezone.now() > phone.date_end:
        messages.error(request, _('The tariff has expired ') + str(phone.date_end))
        return redirect("waba")
    return render(request, 'waba/phone.html', {
        'phone': phone,
        'templates': templates,
        'phone_status_json': phone_status_json,
        'phone_status_error': phone_status_error,
        'ctwa_page_obj': ctwa_page_obj,
        'ctwa_q': ctwa_query,
    })


@login_required
def broadcast_page(request):
    phones = Phone.objects.filter(owner=request.user).select_related("waba")
    templates_data_by_phone = {}
    templates_by_phone = {}
    for phone in phones:
        tqs = Template.objects.filter(waba=phone.waba).prefetch_related(
            "components__named_params",
            "components__positional_params",
            "components__buttons__named_params",
            "components__buttons__positional_params",
        )
        templates_by_phone[phone.id] = tqs
        tdata = []
        for template in tqs:
            components_data = []
            for component in template.components.order_by("index", "id"):
                buttons_data = []
                for button in component.buttons.order_by("index", "id"):
                    buttons_data.append({
                        "id": button.id,
                        "type": button.type,
                        "index": button.index,
                        "named_params": [
                            {"name": p.name} for p in button.named_params.order_by("id")
                        ],
                        "positional_params": [
                            {"position": p.position} for p in button.positional_params.order_by("position", "id")
                        ],
                    })
                components_data.append({
                    "id": component.id,
                    "type": component.type,
                    "format": component.format,
                    "index": component.index,
                    "text": component.text,
                    "named_params": [
                        {"name": p.name} for p in component.named_params.order_by("id")
                    ],
                    "positional_params": [
                        {"position": p.position} for p in component.positional_params.order_by("position", "id")
                    ],
                    "buttons": buttons_data,
                })
            tdata.append({
                "id": template.id,
                "label": f"{template.name} ({template.lang})",
                "lang": template.lang,
                "components": components_data,
            })
        templates_data_by_phone[phone.id] = tdata

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'send_message':
            phone_id = request.POST.get('phone_id')
            template_id = request.POST.get('template')
            recipient_phones_raw = request.POST.get('recipient_phone') or ""
            recipients = [p.strip() for p in recipient_phones_raw.strip().splitlines() if p.strip()]
            if not recipients:
                messages.error(request, _("Please provide at least one recipient phone number."))
                return redirect('broadcast-page')
            try:
                phone = phones.filter(id=phone_id).first()
                if not phone:
                    messages.error(request, _("Phone not found"))
                    return redirect('broadcast-page')
                template_obj = templates_by_phone.get(phone.id, Template.objects.none()).filter(id=template_id).first()
                if not template_obj:
                    messages.error(request, _("Template not found"))
                    return redirect('broadcast-page')

                components_payload = waba_utils.build_template_components_payload(
                    template_obj, request.POST, request.FILES, phone
                )

                schedule_raw = (request.POST.get("schedule_at") or "").strip()
                schedule_date = (request.POST.get("schedule_date") or "").strip()
                schedule_time = (request.POST.get("schedule_time") or "").strip()
                scheduled_at = None
                if schedule_raw:
                    try:
                        scheduled_at = datetime.fromisoformat(schedule_raw)
                        if timezone.is_naive(scheduled_at):
                            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())
                    except Exception:
                        messages.error(request, _("Invalid schedule time"))
                        return redirect('broadcast-page')
                elif schedule_date:
                    try:
                        time_part = schedule_time or "00:00"
                        scheduled_at = datetime.fromisoformat(f"{schedule_date}T{time_part}")
                        if timezone.is_naive(scheduled_at):
                            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())
                    except Exception:
                        messages.error(request, _("Invalid schedule time"))
                        return redirect('broadcast-page')

                broadcast_name = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
                broadcast_text = waba_utils.build_broadcast_text(template_obj, request.POST)
                broadcast = TemplateBroadcast.objects.create(
                    template=template_obj,
                    phone=phone,
                    owner=request.user,
                    name=broadcast_name,
                    text=broadcast_text,
                    recipients_count=len(recipients),
                    status="pending",
                    scheduled_at=scheduled_at,
                )
                TemplateBroadcastRecipient.objects.bulk_create([
                    TemplateBroadcastRecipient(
                        broadcast=broadcast,
                        recipient_phone=recipient,
                        status="pending",
                    )
                    for recipient in recipients
                ])
                if scheduled_at and scheduled_at > timezone.now():
                    async_result = waba_tasks.send_message.apply_async(
                        args=[template_obj.id, recipients, phone.id],
                        kwargs={"components": components_payload, "broadcast_id": broadcast.id},
                        eta=scheduled_at,
                    )
                    broadcast.scheduled_task_id = async_result.id
                    broadcast.save(update_fields=["scheduled_task_id"])
                    messages.success(request, _('The mailing has been scheduled.'))
                else:
                    waba_tasks.send_message.delay(
                        template_obj.id, recipients, phone.id, components=components_payload, broadcast_id=broadcast.id
                    )
                    messages.success(request, _('The mailing has been added to the queue.'))
                return redirect('broadcast-page')
            except Exception as e:
                messages.error(request, str(e))
                return redirect('broadcast-page')

    broadcasts_qs = TemplateBroadcast.objects.filter(owner=request.user).order_by("-created_at")
    b_from = request.GET.get("broadcast_from")
    b_to = request.GET.get("broadcast_to")
    if b_from:
        broadcasts_qs = broadcasts_qs.filter(created_at__date__gte=b_from)
    if b_to:
        broadcasts_qs = broadcasts_qs.filter(created_at__date__lte=b_to)
    from django.core.paginator import Paginator
    paginator = Paginator(broadcasts_qs, 20)
    b_page = request.GET.get("broadcast_page")
    broadcasts_page = paginator.get_page(b_page)

    return render(request, 'waba/broadcast.html', {
        'phones': phones,
        'templates_data_by_phone': templates_data_by_phone,
        'broadcasts': broadcasts_page,
        'broadcast_from': b_from or "",
        'broadcast_to': b_to or "",
    })


@login_required
def broadcast_details(request, broadcast_id):
    broadcast = get_object_or_404(
        TemplateBroadcast, id=broadcast_id, owner=request.user
    )
    phone = broadcast.phone

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "cancel_broadcast":
            if broadcast.status == "pending":
                if broadcast.scheduled_task_id:
                    try:
                        from celery import current_app
                        current_app.control.revoke(broadcast.scheduled_task_id, terminate=False)
                    except Exception:
                        pass
                broadcast.status = "cancelled"
                broadcast.save(update_fields=["status"])
                TemplateBroadcastRecipient.objects.filter(
                    broadcast=broadcast,
                    status="pending",
                ).update(status="cancelled")
                messages.success(request, _('Broadcast has been cancelled.'))
            return redirect("broadcast-details", broadcast_id=broadcast.id)

    qs = broadcast.recipients.all().order_by("id")
    status = request.GET.get("status")
    query = request.GET.get("q")
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(
            Q(recipient_phone__icontains=query) | Q(wamid__icontains=query)
        )

    from django.core.paginator import Paginator
    paginator = Paginator(qs, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(request, "waba/broadcast_detail.html", {
        "phone": phone,
        "broadcast": broadcast,
        "page_obj": page_obj,
        "status": status or "",
        "q": query or "",
    })


@login_required
def waba_account_details(request, waba_id):
    waba = get_object_or_404(
        Waba.objects.select_related("app").prefetch_related("phones").filter(
            Q(owner=request.user) | Q(phones__owner=request.user)
        ).distinct(),
        waba_id=waba_id,
    )
    phones = waba.phones.filter(owner=request.user).order_by("phone", "id")
    status_json = ""
    status_error = ""

    if request.method == "POST" and request.POST.get("action") == "check_status":
        if not waba.app:
            status_error = _("App is not connected to this WABA account.")
        else:
            try:
                status_data = waba_utils.call_api(
                    waba=waba,
                    endpoint=waba.waba_id,
                    payload={"fields": WABA_STATUS_FIELDS},
                )
                status_json = json.dumps(status_data, ensure_ascii=False, indent=2)
            except Exception as e:
                status_error = str(e)

    return render(request, "waba/account.html", {
        "waba": waba,
        "phones": phones,
        "status_json": status_json,
        "status_error": status_error,
    })


@login_message_required(code="waba")
def waba_view(request):
    connector_service = "waba"
    portals, instances, lines = bitrix_utils.get_instances(request, connector_service)
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
        "request_id": str(uuid.uuid4()),
        "days": days,
        "portals": portals,
        "selected_portal_id": request.session.get('b24_data', {}).get('member_id') if request.session.get('b24_data') else "all",
    })


@login_required
def save_request(request):
    user_id = request.user.id
    request_id = request.GET.get('request-id')

    if user_id and request_id:
        domain = request.get_host().split(':')[0]
        app = App.objects.filter(sites__domain__iexact=domain).first()
        if not app:
            messages.error(request, f"App not found for domain {domain}")
            return redirect("waba")
        redis_client.json().set(request_id, "$", {'user': user_id, "app": app.client_id, "host": domain})
        redis_client.expire(request_id, 7200)
        extras = {
            "version": "v3",
            "featureType": "whatsapp_business_app_onboarding"
        }
        params = {
            'client_id': app.client_id,
            'config_id': app.config_id,
            'response_type': 'code',
            'redirect_uri': f'https://{domain}/waba/callback/',
            'state': request_id,
            'extras': json.dumps(extras)
        }
        url = f'https://www.facebook.com/v{app.api_version}.0/dialog/oauth?{urlencode(params)}'
        return redirect(url)
    else:
        return HttpResponseServerError({'error'})

# https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
@login_required
def facebook_callback(request):
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
        app_id = existing.get('app')
        app = App.objects.filter(client_id=app_id).first()
        if not app:
            messages.error("App not found")
            return redirect('waba')
        redis_client.json().set(request_id, '$.code', code)        
        waba_tasks.add_waba_phone.delay(request_id, app_id)
        messages.success(request, _('The number has been successfully added. It will appear here in a few minutes.'))
        return redirect('waba')
    
    else:
        return HttpResponseBadRequest("Invalid request method")

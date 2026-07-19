import json
import redis
import logging
import ast
import secrets
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db import transaction
from django.db.models import Q, OuterRef, Subquery, CharField
from django.core.paginator import Paginator
from django.urls import reverse

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest
from django.utils import timezone
from django.utils.translation import gettext as _
from django.db.models.functions import Cast
from rest_framework.authtoken.models import Token

from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
from separator.decorators import login_message_required, user_message

import separator.bitrix.utils as bitrix_utils
import separator.bitrix.tasks as bitrix_tasks
from separator.bitrix.models import Line, User as BitrixUser

from .forms import InteractiveForm, PartnerAppForm
from .models import App, Interactive, PartnerApp, Waba, Phone, Template, TemplateBroadcast, TemplateBroadcastRecipient, CtwaEvents
import separator.waba.bitrix as waba_bitrix
import separator.waba.utils as waba_utils
import separator.waba.tasks as waba_tasks

from separator.freepbx.tasks import create_extension_task
from django.conf import settings

logger = logging.getLogger(__name__)
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)
WABA_OPEN_TOKEN_TTL = 300
WABA_STATUS_FIELDS = (
    "name,timezone_id,message_template_namespace,account_review_status,"
    "business_verification_status,country,ownership_type,primary_business_location,"
    "marketing_messages_onboarding_status,owner_business_info"
)
PHONE_STATUS_FIELDS = (
    "display_phone_number,verified_name,status,quality_rating,country_code,"
    "country_dial_code,code_verification_status,account_mode,host_platform,"
    "messaging_limit_tier,is_official_business_account,official_business_account"
)


def build_phone_status_summary(status_data, phone=None):
    summary = {
        "items": [],
        "can_register": False,
        "needs_verification": False,
        "needs_oba_application": False,
        "ok": False,
    }
    status = status_data.get("status")
    code_status = status_data.get("code_verification_status")
    is_official_business_account = status_data.get("is_official_business_account")

    if status and status != "CONNECTED":
        summary["items"].append({
            "level": "warning",
            "title": _("Phone is not registered"),
            "text": _("Current status: %(status)s") % {"status": status},
        })
        summary["can_register"] = True

    if status != "CONNECTED" and code_status and code_status != "VERIFIED" and getattr(phone, "type", None) != "app":
        summary["needs_verification"] = True
        summary["items"].append({
            "level": "warning",
            "title": _("Phone number is not verified"),
            "text": _("Verification status: %(status)s") % {"status": code_status},
        })

    show_oba_form = bool(getattr(getattr(getattr(phone, "waba", None), "app", None), "showObaForm", False))
    if is_official_business_account is False and show_oba_form and getattr(phone, "type", None) != "app":
        summary["needs_oba_application"] = True
        summary["items"].append({
            "level": "warning",
            "title": _("Official Business Account is not enabled"),
            "text": _("You can submit an application for Official Business Account status."),
        })

    if not summary["items"]:
        summary["ok"] = True
    return summary


def phone_verification_session_key(phone):
    return f"waba_phone_verification_requested:{phone.id}"


def phone_verification_language(request):
    language = (getattr(request, "LANGUAGE_CODE", None) or settings.LANGUAGE_CODE or "en_US").replace("-", "_")
    if "_" not in language:
        language = {
            "en": "en_US",
            "ru": "ru_RU",
            "kk": "kk_KZ",
        }.get(language, "en_US")
    return language


def format_meta_user_error(error):
    text = str(error)
    marker = ": "
    if marker in text:
        try:
            data = ast.literal_eval(text.split(marker, 1)[1])
            meta_error = data.get("error", {})
            title = meta_error.get("error_user_title")
            message = meta_error.get("error_user_msg")
            if title and message:
                return f"{title}. {message}"
            return title or message or text
        except Exception:
            pass
    return text


def delete_voximplant(phone):
    if phone.voximplant_id and phone.line_id:
        context = waba_bitrix.get_waba_context_for_phone(phone)
        bitrix_tasks.call_api.delay(context["app_instance"].id, "voximplant.sip.delete", {"CONFIG_ID": phone.voximplant_id})
        phone.voximplant_id = None
        phone.voximplant_reg_id = None


def get_current_sip_server(request):
    domain = request.get_host().split(':')[0]
    app = App.objects.filter(sites__domain__iexact=domain).first()
    if not app or not app.sip_server:
        raise Exception(_("FreePBX Server not connected"))
    return app.sip_server


def ensure_phone_extension(phone):
    if not phone.sip_extensions:
        ext = create_extension_task(phone.id)
        phone.sip_extensions = ext
        phone.save(update_fields=["sip_extensions"])

    if not phone.sip_extensions:
        raise Exception(_("SIP extension creation failed."))

    return phone.sip_extensions


def ensure_voximplant(phone, context, ext):
    if phone.voximplant_id:
        return

    payload = {
        "TITLE": f"{phone.phone} WhatsApp",
        "SERVER": ext.server.domain,
        "LOGIN": ext.number,
        "PASSWORD": ext.password
    }
    resp = bitrix_tasks.call_api(context["app_instance"].id, "voximplant.sip.add", payload)
    result = resp.get("result", {})
    phone.voximplant_id = int(result.get("ID"))
    phone.voximplant_reg_id = int(result.get("REG_ID"))
    phone.save(update_fields=["voximplant_id", "voximplant_reg_id"])

@login_required
def phone_details(request, phone_id):
    phone = Phone.objects.select_related(
        "owner",
        "line__portal",
        "line__connector",
        "waba__app__sip_server",
    ).filter(phone_id=phone_id).first()
    if not phone:
        raise Http404
    if not request.user.is_superuser and phone.owner_id != request.user.id:
        portal_id = phone.line.portal_id if phone.line_id and phone.line else None

        has_admin_access = False
        if phone.owner_id and portal_id and phone.availabletoB24admins:
            has_admin_access = BitrixUser.objects.filter(
                owner=request.user,
                bitrix_id=portal_id,
                admin=True,
                active=True,
            ).exists()

        if not has_admin_access:
            messages.error(request, _("This number is linked to another user."))
            return redirect("waba")
    phone_status_json = ""
    phone_status_error = ""
    phone_status_summary = None
    verification_session_key = phone_verification_session_key(phone)
    verification_code_requested = request.session.pop(verification_session_key, False)
    ctwa_query = request.GET.get("ctwa_q", "").strip()
    ctwa_status = request.GET.get("ctwa_status", "").strip()
    templates_qs = Template.objects.filter(waba=phone.waba)
    templates = list(waba_utils.prefetch_template_components(templates_qs))
    for template in templates:
        template.bitrix_code = waba_utils.build_bitrix_template_code(template)

    templates_data = waba_utils.serialize_templates_for_frontend(templates)

    latest_event_subquery = CtwaEvents.objects.filter(
        ctwa_id=OuterRef("pk")
    ).order_by("-date", "-id")
    ctwa_qs = phone.ctwas.annotate(
        id_text=Cast("id", output_field=CharField()),
        phone_text=Cast("phone", output_field=CharField()),
        source_id_text=Cast("source_id", output_field=CharField()),
        last_event=Subquery(latest_event_subquery.values("event")[:1]),
    ).order_by("-id")
    ctwa_status_options = [
        status
        for status in phone.ctwas.annotate(
            last_event=Subquery(latest_event_subquery.values("event")[:1]),
        ).order_by("last_event").values_list("last_event", flat=True).distinct()
        if status
    ]
    ctwa_has_empty_status = phone.ctwas.annotate(
        last_event=Subquery(latest_event_subquery.values("event")[:1]),
    ).filter(last_event__isnull=True).exists()
    if ctwa_query:
        ctwa_qs = ctwa_qs.filter(
            Q(id_text__icontains=ctwa_query)
            | Q(phone_text__icontains=ctwa_query)
            | Q(source_id_text__icontains=ctwa_query)
        )
    if ctwa_status == "__empty__":
        ctwa_qs = ctwa_qs.filter(last_event__isnull=True)
    elif ctwa_status:
        ctwa_qs = ctwa_qs.filter(last_event=ctwa_status)
    ctwa_page_obj = Paginator(ctwa_qs, 50).get_page(request.GET.get("ctwa_page"))
    active_tab = request.POST.get("tab") or request.GET.get("tab") or "templates"
    if active_tab not in {"templates", "ctwa", "bitrix", "calls", "status"}:
        active_tab = "templates"

    def redirect_to_phone_tab(tab_name):
        return redirect(f"{reverse('phone-details', kwargs={'phone_id': phone.phone_id})}?tab={tab_name}")

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
                return redirect_to_phone_tab("templates")
        elif action == 'update_templates':
            delete_ids = set(request.POST.getlist('delete_templates'))
            if delete_ids:
                to_delete_ids = list(templates_qs.filter(id__in=delete_ids).values_list("id", flat=True))
                for template_id in to_delete_ids:
                    waba_tasks.delete_template.delay(template_id, request.user.id)
                if to_delete_ids:
                    messages.success(request, _("Template deletion queued: %(count)s") % {'count': len(to_delete_ids)})

            allowed_ids = set(request.POST.getlist('available_templates'))
            if delete_ids:
                allowed_ids -= delete_ids
            templates_qs.update(availableInB24=False)
            if allowed_ids:
                templates_qs.filter(id__in=allowed_ids).update(availableInB24=True)

            # Handle default template selection (only one per WABA)
            default_id = request.POST.get('default_template')
            if default_id and default_id not in delete_ids:
                templates_qs.update(default=False)
                templates_qs.filter(id=default_id).update(default=True)

            messages.success(request, _('Template availability updated.'))
            return redirect_to_phone_tab("templates")
        elif action == 'update_calling':
            call_dest = request.POST.get('call_dest')
            allowed_call_dest = {choice[0] for choice in Phone.CALL_DEST}
            if call_dest not in allowed_call_dest:
                messages.error(request, _("Invalid call destination"))
                return redirect_to_phone_tab("calls")

            try:
                previous_calling = {
                    "calling": phone.calling,
                    "call_dest": phone.call_dest,
                    "sip_status": phone.sip_status,
                    "sip_hostname": phone.sip_hostname,
                    "sip_port": phone.sip_port,
                }
                update_fields = {"calling", "call_dest"}
                phone.call_dest = call_dest

                if call_dest == "disabled":
                    phone.calling = "disabled"
                    phone.save(update_fields=list(update_fields))
                    waba_tasks.call_management.delay(phone.id)

                else:
                    phone.calling = "enabled"
                    phone.sip_status = "enabled"
                    update_fields.update({"sip_status", "sip_hostname", "sip_port"})

                    if call_dest == "pbx":
                        sip_hostname = request.POST.get('sip_hostname', '').strip()
                        sip_port = request.POST.get('sip_port')
                        if not sip_hostname or not sip_port:
                            messages.error(request, _("SIP Hostname and SIP Port are required"))
                            return redirect_to_phone_tab("calls")
                        phone.sip_hostname = sip_hostname
                        phone.sip_port = sip_port
                    else:
                        sip_server = phone.waba.app.sip_server if phone.waba and phone.waba.app else None
                        if not sip_server:
                            raise Exception(_("FreePBX Server not connected"))
                        phone.sip_hostname = sip_server.domain
                        phone.sip_port = sip_server.sip_port

                    phone.save(update_fields=list(update_fields))
                    try:
                        waba_tasks.call_management(phone.id)
                    except Exception:
                        for field, value in previous_calling.items():
                            setattr(phone, field, value)
                        phone.save(update_fields=list(previous_calling.keys()))
                        raise

                    if call_dest == "pbx":
                        delete_voximplant(phone)
                        phone.save(update_fields=["voximplant_id", "voximplant_reg_id"])
                    elif call_dest == "ext":
                        ensure_phone_extension(phone)
                        delete_voximplant(phone)
                        phone.save(update_fields=["voximplant_id", "voximplant_reg_id"])
                    elif call_dest == "b24":
                        ext = ensure_phone_extension(phone)
                        context = waba_bitrix.get_waba_context_for_phone(phone)
                        ensure_voximplant(phone, context, ext)

                if call_dest == "disabled":
                    messages.info(request, _("Voice calls feature is disabled"))
                else:
                    messages.success(request, _("Call destination %(dest)s enabled") % {'dest': call_dest})
            except Exception as e:
                if call_dest == "b24":
                    user_message(request, "waba_calling_error", "error")
                messages.error(request, str(e))
            return redirect_to_phone_tab("calls")
        elif action == "update_bitrix":
            sms_service = request.POST.get("sms_service") == "on"
            chat_from_sms = request.POST.get("ChatFromSms") == "on"
            read_receipts = request.POST.get("read_receipts") == "on"
            transcribe_model = request.POST.get("transcribe_model") or phone.transcribe_model
            valid_transcribe_models = {choice[0] for choice in Phone.TRANSCRIBE_MODEL_CHOICES}
            if transcribe_model not in valid_transcribe_models:
                transcribe_model = phone.transcribe_model
            available_in_b24 = request.POST.get("availableInB24") == "on"
            available_to_b24_admins = request.POST.get("availabletoB24admins") == "on"
            sms_service_changed = phone.sms_service != sms_service
            chat_from_sms_changed = phone.ChatFromSms != chat_from_sms
            read_receipts_changed = phone.read_receipts != read_receipts
            transcribe_model_changed = phone.transcribe_model != transcribe_model
            available_in_b24_changed = phone.availableInB24 != available_in_b24
            available_to_b24_admins_changed = phone.availabletoB24admins != available_to_b24_admins

            if (
                sms_service_changed
                or chat_from_sms_changed
                or read_receipts_changed
                or transcribe_model_changed
                or available_in_b24_changed
                or available_to_b24_admins_changed
            ):
                phone.sms_service = sms_service
                phone.ChatFromSms = chat_from_sms
                phone.read_receipts = read_receipts
                phone.transcribe_model = transcribe_model
                phone.availableInB24 = available_in_b24
                phone.availabletoB24admins = available_to_b24_admins
                update_fields = []
                if sms_service_changed:
                    update_fields.append("sms_service")
                if chat_from_sms_changed:
                    update_fields.append("ChatFromSms")
                if read_receipts_changed:
                    update_fields.append("read_receipts")
                if transcribe_model_changed:
                    update_fields.append("transcribe_model")
                if available_in_b24_changed:
                    update_fields.append("availableInB24")
                if available_to_b24_admins_changed:
                    update_fields.append("availabletoB24admins")
                phone.save(update_fields=update_fields)

                if sms_service_changed:
                    try:
                        waba_bitrix.get_waba_context_for_phone(phone)
                    except Exception:
                        messages.warning(
                            request,
                            _("Bitrix portal is not connected for this phone, SMS provider sync was skipped."),
                        )
                    else:
                        transaction.on_commit(lambda: waba_bitrix.sync_waba_sms_sender(phone))

                messages.success(request, _("Bitrix settings updated."))
            else:
                messages.info(request, _("No Bitrix settings were changed."))
            return redirect_to_phone_tab("bitrix")
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
                    phone_status_summary = build_phone_status_summary(status_data, phone=phone)
                    if not phone_status_summary["needs_verification"]:
                        request.session.pop(verification_session_key, None)
                        verification_code_requested = False
                except Exception as e:
                    phone_status_error = str(e)
        elif action == "request_verification_code":
            if not phone.waba or not phone.waba.app:
                messages.error(request, _("App is not connected to this phone WABA account."))
            else:
                code_method = request.POST.get("code_method")
                if code_method not in {"SMS", "VOICE"}:
                    messages.error(request, _("Invalid verification method."))
                    return redirect_to_phone_tab("status")
                try:
                    waba_utils.call_api(
                        waba=phone.waba,
                        endpoint=f"{phone.phone_id}/request_code",
                        method="post",
                        payload={
                            "code_method": code_method,
                            "language": phone_verification_language(request),
                        },
                    )
                    request.session[verification_session_key] = True
                    messages.success(request, _("Verification code has been requested."))
                except Exception as e:
                    messages.error(request, format_meta_user_error(e))
            return redirect_to_phone_tab("status")
        elif action == "verify_phone_code":
            if not phone.waba or not phone.waba.app:
                messages.error(request, _("App is not connected to this phone WABA account."))
            else:
                code = (request.POST.get("verification_code") or "").strip()
                if not code:
                    messages.error(request, _("Enter verification code."))
                    request.session[verification_session_key] = True
                    return redirect_to_phone_tab("status")
                try:
                    waba_utils.call_api(
                        waba=phone.waba,
                        endpoint=f"{phone.phone_id}/verify_code",
                        method="post",
                        payload={"code": code},
                    )
                    request.session.pop(verification_session_key, None)
                    waba_tasks.register_phone.delay(phone.id)
                    messages.success(request, _("Phone number has been verified. Registration has been queued."))
                except Exception as e:
                    request.session[verification_session_key] = True
                    messages.error(request, format_meta_user_error(e))
            return redirect_to_phone_tab("status")
        elif action == "register_phone":
            if not phone.waba or not phone.waba.app:
                messages.error(request, _("App is not connected to this phone WABA account."))
            else:
                try:
                    waba_tasks.register_phone.delay(phone.id)
                    messages.success(request, _("Phone registration has been queued."))
                except Exception as e:
                    messages.error(request, format_meta_user_error(e))
            return redirect_to_phone_tab("status")
        elif action == "submit_oba_application":
            if not phone.waba or not phone.waba.app:
                messages.error(request, _("App is not connected to this phone WABA account."))
            elif not phone.waba.app.showObaForm:
                messages.error(request, _("Official Business Account application is not available."))
            elif phone.type == "app":
                messages.error(request, _("Official Business Account application is not available for WhatsApp Business App numbers."))
            else:
                business_website_url = (request.POST.get("business_website_url") or "").strip()
                primary_country_of_operation = (request.POST.get("primary_country_of_operation") or "").strip()
                if not business_website_url or not primary_country_of_operation:
                    messages.error(request, _("Business website URL and primary country of operation are required."))
                    return redirect_to_phone_tab("status")

                payload = {
                    "business_website_url": business_website_url,
                    "primary_country_of_operation": primary_country_of_operation,
                }
                for field in (
                    "primary_language",
                    "parent_business_or_brand",
                    "additional_supporting_information",
                ):
                    value = (request.POST.get(field) or "").strip()
                    if value:
                        payload[field] = value

                supporting_links = [
                    link.strip()
                    for link in (request.POST.get("supporting_links") or "").replace(",", "\n").splitlines()
                    if link.strip()
                ]
                if supporting_links:
                    if len(supporting_links) < 5 or len(supporting_links) > 10:
                        messages.error(request, _("Supporting links must contain from 5 to 10 URLs."))
                        return redirect_to_phone_tab("status")
                    payload["supporting_links"] = supporting_links

                try:
                    response = waba_utils.call_api(
                        waba=phone.waba,
                        endpoint=f"{phone.phone_id}/official_business_account",
                        method="post",
                        payload=payload,
                    )
                    message = response.get("message") or _("Official Business Account application has been submitted.")
                    messages.success(request, message)
                except Exception as e:
                    messages.error(request, format_meta_user_error(e))
            return redirect_to_phone_tab("status")
    
    if not request.user.is_superuser and phone.date_end and timezone.now() > phone.date_end:
        messages.error(request, _('The tariff has expired ') + str(phone.date_end))
        return redirect("waba")
    return render(request, 'waba/phone.html', {
        'phone': phone,
        'templates': templates,
        'phone_status_json': phone_status_json,
        'phone_status_error': phone_status_error,
        'phone_status_summary': phone_status_summary,
        'verification_code_requested': verification_code_requested,
        'ctwa_page_obj': ctwa_page_obj,
        'ctwa_q': ctwa_query,
        'ctwa_status': ctwa_status,
        'ctwa_status_options': ctwa_status_options,
        'ctwa_has_empty_status': ctwa_has_empty_status,
        'transcribe_model_choices': Phone.TRANSCRIBE_MODEL_CHOICES,
        'active_tab': active_tab,
    })


@login_required
def broadcast_page(request):
    now = timezone.now()
    active_phone_filter = Q(date_end__isnull=True) | Q(date_end__gt=now)
    phones = Phone.objects.filter(owner=request.user).filter(active_phone_filter).select_related("waba")
    templates_data_by_phone = {}
    templates_by_phone = {}
    for phone in phones:
        tqs = waba_utils.prefetch_template_components(Template.objects.filter(waba=phone.waba))
        templates_by_phone[phone.id] = tqs
        templates_data_by_phone[phone.id] = waba_utils.serialize_templates_for_frontend(tqs)

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
                phone = Phone.objects.filter(owner=request.user, id=phone_id).select_related("waba").first()
                if not phone:
                    messages.error(request, _("Phone not found"))
                    return redirect('broadcast-page')
                if phone.date_end and phone.date_end <= now:
                    messages.error(request, _('The tariff has expired ') + str(phone.date_end))
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
                        kwargs={
                            "components": components_payload,
                            "broadcast_id": broadcast.id,
                        },
                        eta=scheduled_at,
                    )
                    broadcast.scheduled_task_id = async_result.id
                    broadcast.save(update_fields=["scheduled_task_id"])
                    messages.success(request, _('The mailing has been scheduled.'))
                else:
                    waba_tasks.send_message.delay(
                        template_obj.id,
                        recipients,
                        phone.id,
                        components=components_payload,
                        broadcast_id=broadcast.id,
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

    status_options = list(
        broadcast.recipients.order_by("status").values_list("status", flat=True).distinct()
    )
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
        "status_options": status_options,
        "status": status or "",
        "q": query or "",
    })


@login_required
def interactive_messages(request):
    portals, _instances, _lines = bitrix_utils.get_instances(request, "waba")
    selected_portal_id = request.GET.get("filter_portal_id", "all")
    selected_portal = None
    if selected_portal_id and selected_portal_id != "all":
        selected_portal = portals.filter(id=selected_portal_id).first()
        if not selected_portal:
            selected_portal_id = "all"

    messages_qs = Interactive.objects.filter(Q(portal__in=portals) | Q(**{"global": True})).select_related("portal")
    if selected_portal:
        messages_qs = messages_qs.filter(Q(portal=selected_portal) | Q(**{"global": True}))
    messages_qs = messages_qs.order_by("name")
    for item in messages_qs:
        item.interactive_shortcode = build_interactive_shortcode(item)

    return render(request, "waba/interactive_list.html", {
        "interactive_messages": messages_qs,
        "portals": portals,
        "selected_portal_id": selected_portal_id,
    })


def build_interactive_shortcode(item):
    shortcode = f"interactive+{item.id}"
    variables = (item.payload or {}).get("variables") or []
    params = []
    for variable in variables:
        name = str(variable.get("name", "")).strip()
        if not name:
            continue
        example = str(variable.get("example", "")).strip() or "-"
        params.append(f"{name}:{example}")
    if params:
        shortcode = f"{shortcode}+{'|'.join(params)}"
    return shortcode


@login_required
def interactive_message_create(request):
    portals, _instances, _lines = bitrix_utils.get_instances(request, "waba")
    form = InteractiveForm(request.POST or None, portals=portals)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.owner = request.user
        item.save()
        messages.success(request, _("Interactive message saved."))
        return redirect("waba-interactive")
    return render(request, "waba/interactive_form.html", {
        "form": form,
        "interactive_message": None,
        "interactive_shortcode": "",
    })


@login_required
def interactive_message_edit(request, message_id):
    portals, _instances, _lines = bitrix_utils.get_instances(request, "waba")
    item = get_object_or_404(Interactive.objects.filter(portal__in=portals).filter(**{"global": False}), id=message_id)
    form = InteractiveForm(request.POST or None, instance=item, portals=portals)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Interactive message saved."))
        return redirect("waba-interactive")
    return render(request, "waba/interactive_form.html", {
        "form": form,
        "interactive_message": item,
        "interactive_shortcode": build_interactive_shortcode(item),
    })


@login_required
def interactive_message_delete(request, message_id):
    portals, _instances, _lines = bitrix_utils.get_instances(request, "waba")
    item = get_object_or_404(Interactive.objects.filter(portal__in=portals).filter(**{"global": False}), id=message_id)
    if request.method == "POST":
        item.delete()
        messages.success(request, _("Interactive message deleted."))
    return redirect("waba-interactive")


@login_required
def waba_account_details(request, waba_id):
    waba = Waba.objects.select_related("app").prefetch_related("phones").filter(waba_id=waba_id).first()
    if not waba:
        raise Http404
    if not request.user.is_superuser and waba.owner_id != request.user.id:
        messages.error(request, _("This waba is linked to another user"))
        return redirect("waba")

    if request.user.is_superuser:
        phones = waba.phones.all().order_by("phone", "id")
    else:
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
    portals, instances, _lines = bitrix_utils.get_instances(request, connector_service)
    if not instances:
        user_message(request, "waba_install")

    if request.method == "POST" and "filter_portal_id" in request.POST:
        filter_portal_id = request.POST.get("filter_portal_id")
        if filter_portal_id in {"all", "free"}:
            request.session["waba_portal_filter"] = filter_portal_id
        elif filter_portal_id:
            selected_portal = portals.filter(id=filter_portal_id).first()
            if selected_portal:
                request.session["waba_portal_filter"] = str(selected_portal.id)
        return redirect('waba')

    selected_portal_id = request.session.get("waba_portal_filter", "all")
    selected_portal = None
    show_free_numbers = selected_portal_id == "free"
    if selected_portal_id and selected_portal_id not in {"all", "free"}:
        selected_portal = portals.filter(id=selected_portal_id).first()
        if not selected_portal:
            selected_portal_id = "all"
            request.session.pop("waba_portal_filter", None)

    if selected_portal:
        phones = Phone.objects.filter(
            Q(line__portal=selected_portal) | Q(owner=request.user, line__isnull=True)
        ).distinct()
        instances = instances.filter(portal=selected_portal)
    elif show_free_numbers:
        phones = Phone.objects.filter(owner=request.user, line__isnull=True)
    else:
        phones = Phone.objects.filter(
            Q(line__portal__in=portals) | Q(owner=request.user)
        ).distinct()
    phones = phones.select_related("line", "line__portal", "line__connector", "sip_extensions").order_by("phone", "id")

    lines = (
        Line.objects.filter(portal__in=portals)
        .select_related("portal", "connector")
        .order_by("portal__domain", "name", "line_id")
    )
    if selected_portal:
        lines = lines.filter(portal=selected_portal)

    linked_phone_by_line = {
        phone.line_id: phone
        for phone in Phone.objects.filter(line__portal__in=portals)
        .select_related("line", "line__portal")
        .order_by("phone", "id")
        if phone.line_id
    }
    for line in lines:
        line.linked_waba_phone = linked_phone_by_line.get(line.id)
        line.status_label = waba_bitrix.line_status_label(line)

    if request.method == "POST":
        days = request.POST.get('days')
        if days:
            request.session['waba_days'] = days
        else:
            phone_id = request.POST.get("phone_id")
            line_id = request.POST.get("line_id")
            phone = get_object_or_404(Phone, id=phone_id)
            try:
                waba_bitrix.relink_waba_phone(phone, line_id)
                messages.success(request, _("Line connected"))
            except Exception as e:
                messages.error(request, str(e))
            return redirect("waba")
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
    domain = request.get_host().split(':')[0]
    waba_app = App.objects.filter(sites__domain__iexact=domain).first()
    waba_open_url = ""
    if waba_app and waba_app.auth_flow == App.AuthFlow.POPUP:
        token = secrets.token_urlsafe(32)
        redis_client.setex(f"waba_open_token:{token}", WABA_OPEN_TOKEN_TTL, request.user.id)
        waba_open_url = f"{reverse('waba-open')}?{urlencode({'token': token})}"
    return render(request, "waba/list.html", {
        "phones": phones,
        "waba_lines": lines,
        "instances": instances,
        "days": days,
        "portals": portals,
        "selected_portal_id": selected_portal_id,
        "waba_auth_flow": waba_app.auth_flow if waba_app else "",
        "waba_open_url": waba_open_url,
    })


def waba_open(request):
    token = request.GET.get("token")
    user_id = redis_client.get(f"waba_open_token:{token}") if token else None
    if not user_id:
        return redirect(f"{settings.LOGIN_URL}?next={reverse('waba')}")

    redis_client.delete(f"waba_open_token:{token}")
    user = get_user_model().objects.filter(id=int(user_id)).first()
    if not user:
        return redirect(f"{settings.LOGIN_URL}?next={reverse('waba')}")

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("waba")


def build_redirect_with_params(url, params):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value is not None})
    return urlunparse(parsed._replace(query=urlencode(query)))


@login_required
def partner_apps(request):
    if not request.user.integrator:
        messages.error(request, _("Only integrators can manage partner applications."))
        return redirect("waba")

    token = Token.objects.get_or_create(user=request.user)[0]

    if request.method == "POST" and request.POST.get("action") == "reset_token":
        Token.objects.filter(user=request.user).delete()
        Token.objects.create(user=request.user)
        messages.success(request, _("API token has been reset."))
        return redirect("waba-partner")

    if request.method == "POST" and request.POST.get("action") == "create_partner_app":
        form = PartnerAppForm(request.POST)
        if form.is_valid():
            domain = request.get_host().split(':')[0]
            app = App.objects.filter(sites__domain__iexact=domain).first()
            if not app:
                messages.error(request, f"App not found for domain {domain}")
            else:
                partner_app = form.save(commit=False)
                partner_app.owner = request.user
                partner_app.app = app
                partner_app.save()
                messages.success(request, _("Partner application has been created."))
                return redirect("waba-partner")
    else:
        form = PartnerAppForm(initial={"active": True})

    apps = PartnerApp.objects.filter(owner=request.user).select_related("app").order_by("-created_at")
    return render(request, "waba/partner.html", {
        "form": form,
        "partner_apps": apps,
        "token": token.key,
    })


@login_required
def partner_app_edit(request, partner_app_id):
    if not request.user.integrator:
        messages.error(request, _("Only integrators can manage partner applications."))
        return redirect("waba")

    partner_app = get_object_or_404(PartnerApp, id=partner_app_id, owner=request.user)
    if request.method == "POST":
        if request.POST.get("action") == "delete":
            partner_app.delete()
            messages.success(request, _("Partner application has been deleted."))
            return redirect("waba-partner")
        else:
            form = PartnerAppForm(request.POST, instance=partner_app)
            if form.is_valid():
                form.save()
                messages.success(request, _("Partner application has been updated."))
                return redirect("waba-partner")
    else:
        form = PartnerAppForm(instance=partner_app)

    return render(request, "waba/partner_edit.html", {
        "form": form,
        "partner_app": partner_app,
    })


# https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
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
        if not code:
            messages.error(request, "Authorization code is missing")
            return redirect('waba')
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
            messages.error(request, "App not found")
            return redirect('waba')

        state_lock_key = f"{request_id}:used"
        if not redis_client.set(state_lock_key, "1", nx=True, ex=7200):
            messages.error(request, "Request has already been used")
            return redirect('waba')

        partner_app_id = existing.get("partner_app_id")
        redis_client.json().set(request_id, '$.code', code)
        if partner_app_id:
            partner_app = PartnerApp.objects.filter(id=partner_app_id, active=True).first()
            if not partner_app:
                messages.error(request, "Partner app not found")
                return redirect('waba')
            try:
                _current_data, _app, access_token, wabas = waba_tasks.exchange_embedded_signup_code(request_id, app_id)
            except Exception as e:
                return redirect(build_redirect_with_params(partner_app.redirect_url, {"error": str(e)}))
            if not wabas:
                return redirect(build_redirect_with_params(partner_app.redirect_url, {"error": "waba_not_found"}))

            waba_id = wabas[0]
            redis_client.json().set(request_id, '$.access_token', access_token)
            redis_client.json().set(request_id, '$.wabas', wabas)
            waba_tasks.add_partner_waba_phone.delay(request_id, app_id)
            return redirect(build_redirect_with_params(partner_app.redirect_url, {"waba_id": waba_id}))

        waba_tasks.add_waba_phone.delay(request_id, app_id)
        messages.success(request, _('The number has been successfully added. It will appear here in a few minutes.'))
        return redirect('waba')
    
    else:
        return HttpResponseBadRequest("Invalid request method")

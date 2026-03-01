import json
import re

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext as _

from separator.waba.models import Phone, Template
from separator.olx.models import OlxUser
from separator.waweb.models import Session
import separator.waba.utils as waba_utils

from .crest import call_method
from .utils import get_app, parse_template_code, connect_line
from .models import AppInstance, Bitrix, Line, Connector

def settings_connector(request, user):
    data = request.POST
    domain = request.GET.get("DOMAIN")
    placement_options = data.get("PLACEMENT_OPTIONS")
    instance_id = request.GET.get("inst")

    placement_data = {}
    if placement_options:
        try:
            placement_data = json.loads(placement_options)
        except (TypeError, ValueError):
            placement_data = {}

    line_id = data.get("line_id") or placement_data.get("LINE")
    connector_code = data.get("connector_code") or placement_data.get("CONNECTOR")

    app_instance = AppInstance.objects.filter(id=instance_id).first()
    if not app_instance:
        return HttpResponse("app not found")
    portal = Bitrix.objects.filter(domain=domain).first()
    if not portal:
        return HttpResponse("bitrix not found")
    connector = Connector.objects.filter(code=connector_code).first()
    if not connector:
        return HttpResponse("connector not found")
    line, created = Line.objects.get_or_create(
        line_id=line_id,
        portal=portal,
        connector=connector,
        app_instance=app_instance,
        owner=app_instance.owner
    )
    configs = {
        "waba": {
            "queryset": Phone.objects.filter(owner=user, line__isnull=True).order_by("phone"),
            "id_field": "phone_id",
            "label": lambda obj: obj.phone,
            "not_found": _("phone not found"),
        },
        "olx": {
            "queryset": OlxUser.objects.filter(owner=user, line__isnull=True).order_by("name", "olx_id"),
            "id_field": "olx_id",
            "label": lambda obj: obj.name or obj.olx_id,
            "not_found": _("olx user not found"),
        },
        "waweb": {
            "queryset": Session.objects.filter(owner=user, line__isnull=True).order_by("phone", "session"),
            "id_field": "session_id",
            "label": lambda obj: obj.phone or obj.session,
            "not_found": _("session not found"),
        },
    }

    config = configs.get(connector.service)
    if config:
        items = [{"id": obj.id, "label": config["label"](obj)} for obj in config["queryset"]]
        selected_id = data.get(config["id_field"])
        if selected_id:
            selected = next((obj for obj in config["queryset"] if str(obj.id) == str(selected_id)), None)
            if not selected:
                return HttpResponse(config["not_found"], status=404)
            if not line_id:
                return HttpResponse(_("line not found"), status=404)
            connect_line(request, line.id, selected, connector.service)
            return HttpResponse(_("Connected"))
        return render(
            request,
            "bitrix/placements/setting_connector.html",
            {
                "items": items,
                "item_id_field": config["id_field"],
                "line_id": line_id,
                "connector_code": connector_code,
                "auth_id": data.get("AUTH_ID"),
                "connector_service": connector.service,
                "user": user,
            },
        )

    return HttpResponse(
        f"Set Line Settings: https://{app_instance.app.site}/portals/"
    )


class WabaPlacementModule:
    def handle(self, placement_type, request):
        if placement_type == "send_template":
            return self.send_template(request)
        return HttpResponse(_("unsupported waba placement type"), status=404)

    @staticmethod
    def _request_data(request):
        return request.POST if request.method == "POST" else request.GET

    @staticmethod
    def _parse_placement_options(raw_value):
        if not raw_value:
            return {}
        try:
            return json.loads(raw_value)
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _extract_phone_candidates(raw_phone):
        if not raw_phone:
            return []

        values = []
        if isinstance(raw_phone, list):
            for item in raw_phone:
                if isinstance(item, dict):
                    value = item.get("VALUE") or item.get("value")
                    if value:
                        values.append(str(value))
                elif item:
                    values.append(str(item))
        elif isinstance(raw_phone, dict):
            value = raw_phone.get("VALUE") or raw_phone.get("value")
            if value:
                values.append(str(value))
        elif raw_phone:
            values.append(str(raw_phone))

        normalized = []
        for value in values:
            digits = re.sub(r"\D", "", value)
            if digits:
                normalized.append(digits)
        return normalized

    @staticmethod
    def _resolve_appinstance(data):
        auth_id = data.get("AUTH_ID")
        member_id = data.get("member_id") or data.get("MEMBER_ID")
        app = get_app(auth_id)
        portal = Bitrix.objects.filter(member_id=member_id).first()
        if not portal:
            return None, None, None
        appinstance = AppInstance.objects.filter(app=app, portal=portal).first()
        return app, portal, appinstance

    def _collect_recipient_phones(self, appinstance, placement, placement_options):
        entity_id = (
            placement_options.get("ID")
            or placement_options.get("ENTITY_ID")
            or placement_options.get("entityId")
        )
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError):
            entity_id = None

        if not entity_id:
            return []

        phones = []

        def add_phones(raw_phone):
            for phone in self._extract_phone_candidates(raw_phone):
                if phone not in phones:
                    phones.append(phone)

        def get_contact_phones(contact_id):
            try:
                contact = call_method(appinstance, "crm.contact.get", {"id": int(contact_id)}).get("result", {})
            except Exception:
                return []
            return self._extract_phone_candidates(contact.get("PHONE"))

        def get_company_phones(company_id):
            try:
                company = call_method(appinstance, "crm.company.get", {"id": int(company_id)}).get("result", {})
            except Exception:
                return []
            return self._extract_phone_candidates(company.get("PHONE"))

        placement = placement or ""

        try:
            if "_CONTACT_" in placement:
                add_phones(call_method(appinstance, "crm.contact.get", {"id": entity_id}).get("result", {}).get("PHONE"))
            elif "_COMPANY_" in placement:
                add_phones(call_method(appinstance, "crm.company.get", {"id": entity_id}).get("result", {}).get("PHONE"))
            elif "_LEAD_" in placement:
                add_phones(call_method(appinstance, "crm.lead.get", {"id": entity_id}).get("result", {}).get("PHONE"))
            elif "_DEAL_" in placement:
                deal = call_method(appinstance, "crm.deal.get", {"id": entity_id}).get("result", {})
                contact_id = deal.get("CONTACT_ID")
                company_id = deal.get("COMPANY_ID")
                for phone in get_contact_phones(contact_id) + get_company_phones(company_id):
                    if phone not in phones:
                        phones.append(phone)
            elif "_QUOTE_" in placement:
                quote = call_method(appinstance, "crm.quote.get", {"id": entity_id}).get("result", {})
                contact_id = quote.get("CONTACT_ID")
                company_id = quote.get("COMPANY_ID")
                for phone in get_contact_phones(contact_id) + get_company_phones(company_id):
                    if phone not in phones:
                        phones.append(phone)
        except Exception:
            return phones

        return phones

    @staticmethod
    def _resolve_entity_context(placement, placement_options):
        entity_id = (
            placement_options.get("ID")
            or placement_options.get("ENTITY_ID")
            or placement_options.get("entityId")
        )
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError):
            entity_id = None

        placement = placement or ""
        entity_type_code = None
        dynamic_match = re.match(r"CRM_DYNAMIC_(\d+)_", placement)
        if dynamic_match:
            entity_type_code = f"dynamic_{dynamic_match.group(1)}"
        elif "_LEAD_" in placement:
            entity_type_code = "lead"
        elif "_DEAL_" in placement:
            entity_type_code = "deal"
        elif "_CONTACT_" in placement:
            entity_type_code = "contact"
        elif "_COMPANY_" in placement:
            entity_type_code = "company"
        elif "_QUOTE_" in placement:
            entity_type_code = "quote"
        elif "_SMART_INVOICE_" in placement:
            entity_type_code = "smart_invoice"

        return entity_id, entity_type_code

    def send_template(self, request):
        data = self._request_data(request)
        action = data.get("action")

        try:
            app, portal, appinstance = self._resolve_appinstance(data)
        except Exception as exc:
            if action:
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
            return HttpResponse(str(exc), status=400)

        if not appinstance:
            if action:
                return JsonResponse({"ok": False, "error": _("appinstance not found")}, status=400)
            return HttpResponse(_("appinstance not found"), status=400)

        sender_phones = Phone.objects.filter(
            app_instance=appinstance,
            waba__isnull=False,
        ).select_related("waba").order_by("phone")

        def serialize_templates(templates_qs):
            templates_qs = templates_qs.prefetch_related(
                "components__named_params",
                "components__positional_params",
                "components__buttons__named_params",
                "components__buttons__positional_params",
            )
            out = []
            for template in templates_qs:
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
                out.append({
                    "id": str(template.id),
                    "label": f"{template.name} ({template.lang})",
                    "lang": template.lang,
                    "components": components_data,
                })
            return out

        if action == "get_templates":
            sender_phone_id = data.get("sender_phone_id")
            sender_phone = sender_phones.filter(id=sender_phone_id).first()
            if not sender_phone:
                return JsonResponse({"ok": False, "error": _("sender phone not found")}, status=400)

            templates = Template.objects.filter(
                waba=sender_phone.waba,
                status="APPROVED",
                availableInB24=True,
            ).order_by("name", "lang")

            return JsonResponse(
                {
                    "ok": True,
                    "templates": serialize_templates(templates),
                }
            )

        if action == "send":
            sender_phone_id = data.get("sender_phone_id")
            template_id = data.get("template_id")
            recipient_phone = re.sub(r"\D", "", data.get("phone", ""))
            entity_id_raw = data.get("entity_id")
            entity_type_code = (data.get("entity_type_code") or "").strip().lower()
            bitrix_user_id = data.get("bitrix_user_id")
            sender_phone = sender_phones.filter(id=sender_phone_id).first()
            if not sender_phone:
                return JsonResponse({"ok": False, "error": _("sender phone not found")}, status=400)

            template = Template.objects.filter(id=template_id, availableInB24=True).first()
            if not template:
                return JsonResponse({"ok": False, "error": _("template not found")}, status=400)
            if template.waba_id and sender_phone.waba_id and template.waba_id != sender_phone.waba_id:
                return JsonResponse(
                    {"ok": False, "error": _("template does not belong to selected sender number")},
                    status=400,
                )
            if not recipient_phone:
                return JsonResponse({"ok": False, "error": _("recipient phone is required")}, status=400)

            sender_digits = re.sub(r"\D", "", sender_phone.phone or "")
            if not sender_digits:
                return JsonResponse({"ok": False, "error": _("selected sender number is invalid")}, status=400)

            components_payload = waba_utils.build_template_components_payload(
                template, data, request.FILES, sender_phone
            )
            message = {
                "messaging_product": "whatsapp",
                "to": recipient_phone,
                "type": "template",
                "template": {
                    "name": template.name,
                    "language": {"code": template.lang},
                },
            }
            if bitrix_user_id:
                message["biz_opaque_callback_data"] = {"bitrix_user_id": str(bitrix_user_id)}
            if components_payload:
                message["template"]["components"] = components_payload

            send_result = waba_utils.send_message(appinstance, message, phone_num=sender_digits)
            if isinstance(send_result, dict) and send_result.get("error"):
                return JsonResponse({"ok": False, "error": send_result.get("message") or _("send failed")}, status=400)

            entity_id = None
            try:
                entity_id = int(entity_id_raw)
            except (TypeError, ValueError):
                entity_id = None

            if entity_id and entity_type_code:
                try:
                    sent_json = json.dumps(message, ensure_ascii=False, default=str)
                    response_json = json.dumps(send_result, ensure_ascii=False, default=str)
                    comment_text = (
                        f"{_('WABA template sent')}\n"
                        f"{_('Sender')}: {sender_phone.phone}\n"
                        f"{_('Recipient')}: +{recipient_phone}\n"
                        f"{_('Template')}: {template.name} ({template.lang})\n"
                        f"{_('Sent payload')}:\n"
                        f"{sent_json[:3500]}\n"
                        f"{_('Facebook response')}:\n"
                        f"{response_json[:3500]}"
                    )
                    call_method(
                        appinstance,
                        "crm.timeline.comment.add",
                        {
                            "fields": {
                                "ENTITY_ID": entity_id,
                                "ENTITY_TYPE": entity_type_code,
                                "COMMENT": comment_text,
                            }
                        },
                    )
                except Exception:
                    pass

            return JsonResponse({"ok": True})

        placement = data.get("PLACEMENT")
        placement_options = self._parse_placement_options(data.get("PLACEMENT_OPTIONS"))
        entity_id, entity_type_code = self._resolve_entity_context(placement, placement_options)
        recipient_phones = self._collect_recipient_phones(appinstance, placement, placement_options)

        selected_sender = sender_phones.first()
        templates_data_by_phone = {}
        if sender_phones:
            for sender in sender_phones:
                if sender.waba_id:
                    tqs = Template.objects.filter(
                        waba=sender.waba,
                        status="APPROVED",
                        availableInB24=True,
                    ).order_by("name", "lang")
                else:
                    tqs = Template.objects.none()
                templates_data_by_phone[str(sender.id)] = serialize_templates(tqs)

        context = {
            "auth_id": data.get("AUTH_ID") or "",
            "refresh_id": data.get("REFRESH_ID") or "",
            "member_id": data.get("member_id") or data.get("MEMBER_ID") or "",
            "sender_phones": [{"id": str(item.id), "label": item.phone or str(item.id)} for item in sender_phones],
            "templates_data_by_phone": templates_data_by_phone,
            "recipient_phones": [{"value": phone, "label": f"+{phone}"} for phone in recipient_phones],
            "initial_sender_phone_id": str(selected_sender.id) if selected_sender else "",
            "entity_id": entity_id or "",
            "entity_type_code": entity_type_code or "",
        }
        return render(request, "bitrix/placements/waba_send_template.html", context)

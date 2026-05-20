import json
import re
import requests

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _

from separator.waba.models import Phone, Template
from separator.olx.models import OlxUser
from separator.waweb.models import Session
import separator.waba.utils as waba_utils

from .crest import call_method
from .utils import get_app, connect_line, format_waba_error
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
    member_id = data.get("member_id")

    app_instance = AppInstance.objects.filter(id=instance_id).first()
    if not app_instance:
        return HttpResponse("app not found")
    portal = Bitrix.objects.filter(domain=domain).first()
    if not portal:
        return HttpResponse("bitrix not found")
    connector = Connector.objects.filter(code=connector_code).first()
    if not connector:
        return HttpResponse("connector not found")
    line = Line.objects.filter(line_id=line_id, portal=portal).order_by("id").first()
    if not line:
        line = Line.objects.create(line_id=line_id, portal=portal, connector=connector)
    elif line.connector_id != connector.id:
        line.connector = connector
        line.save(update_fields=["connector"])
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
                "member_id": member_id,
                "connector_service": connector.service,
                "add_number_url": WabaPlacementModule._portal_app_url(app_instance, data.get("AUTH_ID")),
                "user": user,
            },
        )

    return HttpResponse(
        f"Set Line Settings: https://{app_instance.app.site}/portals/"
    )


class WabaPlacementModule:
    BLOCKS = [
        {"id": "templates", "label": _("Templates")},
        {"id": "block_users", "label": _("Block users")},
        {"id": "call_permission", "label": _("Calls")},
    ]
    BLOCK_ACTION_METHODS = {
        "block_status": "GET",
        "block_add": "POST",
        "block_remove": "DELETE",
    }
    CALL_ACTIONS = {"call_permission_status", "call_start"}

    def __init__(self, app=None, portal=None, appinstance=None):
        self.app = app
        self.portal = portal
        self.appinstance = appinstance

    def handle(self, request):
        return self.send_template(request)

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
        member_id = data.get("member_id")
        app = get_app(auth_id)
        portal = Bitrix.objects.filter(member_id=member_id).first()
        if not portal:
            return None, None, None
        appinstance = AppInstance.objects.filter(app=app, portal=portal).first()
        return app, portal, appinstance

    def _resolve_context(self, data):
        if self.appinstance:
            return self.app, self.portal, self.appinstance

        app = self.app
        portal = self.portal
        if not app:
            app = get_app(data.get("AUTH_ID"))
        if not portal:
            portal = Bitrix.objects.filter(member_id=data.get("member_id")).first()
        if not portal:
            return app, None, None

        appinstance = AppInstance.objects.filter(app=app, portal=portal).first()
        self.app = app
        self.portal = portal
        self.appinstance = appinstance
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

    @staticmethod
    def _normalize_phone(value):
        return re.sub(r"\D", "", str(value or ""))

    @staticmethod
    def _normalize_block_user_phone(value):
        digits = re.sub(r"\D", "", str(value or ""))
        return f"+{digits}" if digits else ""

    @staticmethod
    def _get_sender_phones(appinstance):
        return Phone.objects.filter(
            app_instance=appinstance,
            waba__isnull=False,
            availableInB24=True,
        ).select_related("waba", "waba__app").order_by("phone")

    def _get_sender_phone(self, sender_phones, sender_phone_id):
        if not sender_phone_id:
            return None
        try:
            return sender_phones.filter(id=sender_phone_id).first()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _templates_for_sender(sender_phone):
        if not sender_phone or not sender_phone.waba_id:
            return Template.objects.none()
        return waba_utils.prefetch_template_components(
            Template.objects.filter(
                waba=sender_phone.waba,
                status="APPROVED",
                availableInB24=True,
            ).order_by("name", "lang")
        )

    def _block_users_api(self, sender_phone, method, user_phone=None):
        if not sender_phone:
            return {"error": _("sender phone not found")}, 400
        if not sender_phone.waba or not sender_phone.waba.app:
            return {"error": _("waba not found")}, 400
        if sender_phone.date_end and sender_phone.date_end <= timezone.now():
            return {"error": _("phone tariff expired")}, 400
        if not sender_phone.phone_id:
            return {"error": _("phone_number_id not found")}, 400

        base_url = settings.FACEBOOK_API_URL.rstrip("/")
        url = f"{base_url}/v{sender_phone.waba.app.api_version}.0/{sender_phone.phone_id}/block_users"
        headers = {"Authorization": f"Bearer {sender_phone.waba.access_token}"}
        payload = None

        if method in {"POST", "DELETE"}:
            normalized_phone = self._normalize_block_user_phone(user_phone)
            if not normalized_phone:
                return {"error": _("recipient phone is required")}, 400
            payload = {
                "messaging_product": "whatsapp",
                "block_users": [{"user": normalized_phone}],
            }

        try:
            response = requests.request(method, url, headers=headers, json=payload, timeout=30)
        except requests.RequestException as exc:
            return {"error": str(exc)}, 502
        try:
            result = response.json() if response.content else {}
        except ValueError:
            result = {"raw": response.text}
        return result, response.status_code

    def _call_permission_api(self, sender_phone, user_phone=None):
        if not sender_phone:
            return {"error": _("sender phone not found")}, 400
        if not sender_phone.waba or not sender_phone.waba.app:
            return {"error": _("waba not found")}, 400
        if sender_phone.date_end and sender_phone.date_end <= timezone.now():
            return {"error": _("phone tariff expired")}, 400
        if not sender_phone.phone_id:
            return {"error": _("phone_number_id not found")}, 400

        normalized_phone = self._normalize_block_user_phone(user_phone)
        if not normalized_phone:
            return {"error": _("recipient phone is required")}, 400

        base_url = settings.FACEBOOK_API_URL.rstrip("/")
        url = f"{base_url}/v{sender_phone.waba.app.api_version}.0/{sender_phone.phone_id}/call_permissions"
        headers = {"Authorization": f"Bearer {sender_phone.waba.access_token}"}
        params = {"user_wa_id": normalized_phone}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            return {"error": str(exc)}, 502
        try:
            result = response.json() if response.content else {}
        except ValueError:
            result = {"raw": response.text}
        return result, response.status_code

    def _ensure_voximplant_reg_id(self, sender_phone):
        if not sender_phone or not sender_phone.app_instance:
            return None
        if sender_phone.voximplant_reg_id:
            return sender_phone.voximplant_reg_id
        if not sender_phone.voximplant_id:
            return None

        result = call_method(sender_phone.app_instance, "voximplant.sip.get", {
            "FILTER": {
                "ID": sender_phone.voximplant_id,
            }
        })
        items = result.get("result") or []
        if not items:
            return None

        reg_id = items[0].get("REG_ID")
        if not reg_id:
            return None

        sender_phone.voximplant_reg_id = int(reg_id)
        sender_phone.save(update_fields=["voximplant_reg_id"])
        return sender_phone.voximplant_reg_id

    @staticmethod
    def _bitrix_telephony_not_connected_error(sender_phone):
        phone = (getattr(sender_phone, "phone", "") or "").strip()
        if not phone:
            phone = _("selected sender number")
        return {
            "error": _("{phone} is not connected to Bitrix telephony").format(phone=phone),
        }

    @staticmethod
    def _can_start_call(permission_result):
        actions = permission_result.get("actions") if isinstance(permission_result, dict) else []
        if not isinstance(actions, list):
            return False
        start_call_action = next(
            (item for item in actions if item and item.get("action_name") == "start_call"),
            None,
        )
        return bool(start_call_action and start_call_action.get("can_perform_action"))

    def _start_voximplant_callback(self, sender_phone, user_phone=None):
        if not sender_phone:
            return {"error": _("sender phone not found")}, 400
        if not sender_phone.app_instance:
            return {"error": _("appinstance not found")}, 400

        permission_result, permission_status_code = self._call_permission_api(sender_phone, user_phone)
        if not (200 <= permission_status_code < 300):
            return permission_result, permission_status_code
        if isinstance(permission_result, dict) and permission_result.get("error"):
            return permission_result, 400
        if not self._can_start_call(permission_result):
            if isinstance(permission_result, dict):
                permission_result = dict(permission_result)
                permission_result["error"] = _("user did not allow calls")
            else:
                permission_result = {"error": _("user did not allow calls")}
            return permission_result, 400

        reg_id = self._ensure_voximplant_reg_id(sender_phone)
        if not reg_id:
            return self._bitrix_telephony_not_connected_error(sender_phone), 400

        normalized_phone = self._normalize_phone(user_phone)
        if not normalized_phone:
            return {"error": _("recipient phone is required")}, 400

        payload = {
            "FROM_LINE": f"reg{reg_id}",
            "TO_NUMBER": normalized_phone,
        }
        try:
            result = call_method(sender_phone.app_instance, "voximplant.callback.start", payload)
            return result, 200
        except Exception as exc:
            return {"error": str(exc)}, 400

    @staticmethod
    def _is_block_users_success(result, status_code, method):
        if not (200 <= status_code < 300):
            return False
        if not isinstance(result, dict):
            return True
        if result.get("errors"):
            return False
        failed_users = ((result.get("block_users") or {}).get("failed_users")) or []
        if failed_users:
            return False
        if method == "POST":
            return bool(((result.get("block_users") or {}).get("added_users")) or [])
        if method == "DELETE":
            return bool(((result.get("block_users") or {}).get("removed_users")) or [])
        return True

    @staticmethod
    def _action_response(payload):
        return JsonResponse(payload, status=200)

    @staticmethod
    def _plain_response(text):
        return HttpResponse(text, status=200)

    def _send_template_action(self, request, data, appinstance, sender_phones):
        sender_phone = self._get_sender_phone(sender_phones, data.get("sender_phone_id"))
        if not sender_phone:
            return self._action_response({"ok": False, "error": _("sender phone not found")})

        recipient_phone = self._normalize_phone(data.get("phone", ""))
        if not recipient_phone:
            return self._action_response({"ok": False, "error": _("recipient phone is required")})

        template = self._templates_for_sender(sender_phone).filter(id=data.get("template_id")).first()
        if not template:
            return self._action_response({"ok": False, "error": _("template not found")})

        try:
            components_payload = waba_utils.build_template_components_payload(
                template, data, request.FILES, sender_phone
            )
        except Exception as exc:
            return self._action_response({"ok": False, "error": str(exc)})

        message = {
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "template",
            "template": {
                "name": template.name,
                "language": {"code": template.lang},
            },
        }
        bitrix_user_id = data.get("bitrix_user_id")
        if bitrix_user_id:
            message["biz_opaque_callback_data"] = {"bitrix_user_id": str(bitrix_user_id)}
        if components_payload:
            message["template"]["components"] = components_payload

        send_result = waba_utils.send_message_from_phone(sender_phone, message, template=template)
        if isinstance(send_result, dict) and send_result.get("error"):
            return self._action_response({"ok": False, "error": format_waba_error(send_result) or _("send failed")})

        self._add_timeline_comment(appinstance, sender_phone, template, recipient_phone, send_result, data)
        return self._action_response({"ok": True, "result": send_result})

    @staticmethod
    def _short_param_value(value, limit=500):
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    @staticmethod
    def _message_id_from_send_result(send_result):
        if not isinstance(send_result, dict):
            return ""
        messages = send_result.get("messages") or []
        if isinstance(messages, list) and messages:
            return messages[0].get("id") or ""
        return ""

    def _template_param_lines(self, template, data):
        lines = []
        components = sorted(template.components.all(), key=lambda item: (item.index, item.id))
        for component in components:
            component_label = component.type or "COMPONENT"
            if component.type != "BUTTONS":
                for param in sorted(component.named_params.all(), key=lambda item: item.id):
                    value = self._short_param_value(data.get(f"param__{component.id}__named__{param.name}"))
                    if value:
                        lines.append(f"{component_label} {param.name}: {value}")
                for param in sorted(component.positional_params.all(), key=lambda item: (item.position, item.id)):
                    value = self._short_param_value(data.get(f"param__{component.id}__pos__{param.position}"))
                    if value:
                        lines.append(f"{component_label} param {param.position}: {value}")

            if component.type == "HEADER" and component.format in ("IMAGE", "VIDEO", "DOCUMENT", "GIF"):
                value = self._short_param_value(data.get(f"media__{component.id}"))
                if value:
                    lines.append(f"{component_label} media: {value}")

            if component.type == "HEADER" and component.format == "LOCATION":
                for field in ("latitude", "longitude", "name", "address"):
                    value = self._short_param_value(data.get(f"location__{component.id}__{field}"))
                    if value:
                        lines.append(f"{component_label} {field}: {value}")

            if component.type == "BUTTONS":
                buttons = sorted(component.buttons.all(), key=lambda item: (item.index, item.id))
                for button in buttons:
                    button_label = f"Button {button.index + 1} {button.type}"
                    for param in sorted(button.named_params.all(), key=lambda item: item.id):
                        value = self._short_param_value(data.get(f"param__btn__{button.id}__named__{param.name}"))
                        if value:
                            lines.append(f"{button_label} {param.name}: {value}")
                    for param in sorted(button.positional_params.all(), key=lambda item: (item.position, item.id)):
                        value = self._short_param_value(data.get(f"param__btn__{button.id}__pos__{param.position}"))
                        if value:
                            lines.append(f"{button_label} param {param.position}: {value}")
        return lines

    def _add_timeline_comment(self, appinstance, sender_phone, template, recipient_phone, send_result, data):
        try:
            entity_id = int(data.get("entity_id"))
        except (TypeError, ValueError):
            return

        entity_type_code = (data.get("entity_type_code") or "").strip().lower()
        if not entity_type_code:
            return

        try:
            comment_lines = [
                _("WABA template sent"),
                f"{_('Sender')}: {sender_phone.phone}",
                f"{_('Recipient')}: +{recipient_phone}",
                f"{_('Template')}: {template.name} ({template.lang})",
            ]
            param_lines = self._template_param_lines(template, data)
            if param_lines:
                comment_lines.append("параметры:")
                comment_lines.extend(param_lines)
            message_id = self._message_id_from_send_result(send_result)
            if message_id:
                comment_lines.append(f"id {message_id}")
            call_method(
                appinstance,
                "crm.timeline.comment.add",
                {
                    "fields": {
                        "ENTITY_ID": entity_id,
                        "ENTITY_TYPE": entity_type_code,
                        "COMMENT": "\n".join(comment_lines),
                    }
                },
            )
        except Exception:
            pass

    def _block_users_action(self, data, sender_phones, action):
        sender_phone = self._get_sender_phone(sender_phones, data.get("sender_phone_id"))
        method = self.BLOCK_ACTION_METHODS[action]
        result, status_code = self._block_users_api(sender_phone, method, data.get("phone"))
        return self._action_response({
            "ok": self._is_block_users_success(result, status_code, method),
            "result": result,
        })

    def _call_action(self, data, sender_phones, action):
        sender_phone = self._get_sender_phone(sender_phones, data.get("sender_phone_id"))
        if action == "call_permission_status":
            result, status_code = self._call_permission_api(sender_phone, data.get("phone"))
        else:
            result, status_code = self._start_voximplant_callback(sender_phone, data.get("phone"))
        return self._action_response({
            "ok": 200 <= status_code < 300 and not (isinstance(result, dict) and result.get("error")),
            "result": result,
        })

    def _dispatch_action(self, request, data, appinstance, sender_phones, action):
        if action == "send":
            return self._send_template_action(request, data, appinstance, sender_phones)
        if action in self.BLOCK_ACTION_METHODS:
            return self._block_users_action(data, sender_phones, action)
        if action in self.CALL_ACTIONS:
            return self._call_action(data, sender_phones, action)
        return self._action_response({"ok": False, "error": _("unknown action")})

    @staticmethod
    def _portal_app_url(appinstance, auth_id=None):
        portal_app_id = None
        if auth_id and appinstance and appinstance.portal:
            try:
                endpoint = f"{appinstance.portal.protocol}://{appinstance.portal.domain}/rest/app.info"
                response = requests.post(endpoint, json={"auth": auth_id}, timeout=10)
                if response.status_code == 200:
                    portal_app_id = (response.json().get("result") or {}).get("ID")
            except Exception:
                portal_app_id = None

        if not portal_app_id:
            try:
                app_info = call_method(appinstance, "app.info", {})
                portal_app_id = (app_info.get("result") or {}).get("ID")
            except Exception:
                portal_app_id = None

        if portal_app_id and appinstance and appinstance.portal:
            return f"{appinstance.portal.protocol}://{appinstance.portal.domain}/marketplace/app/{portal_app_id}/"
        return (appinstance.app.page_url if appinstance and appinstance.app else "") or "/"

    def _render_widget(self, request, data, appinstance, sender_phones):
        placement = data.get("PLACEMENT")
        placement_options = self._parse_placement_options(data.get("PLACEMENT_OPTIONS"))
        entity_id, entity_type_code = self._resolve_entity_context(placement, placement_options)

        senders = list(sender_phones)
        if not senders:
            return render(request, "bitrix/placements/waba.html", {
                "no_sender_app_url": self._portal_app_url(appinstance, data.get("AUTH_ID")),
            })

        recipient_phones = self._collect_recipient_phones(appinstance, placement, placement_options)
        selected_sender = senders[0] if senders else None
        templates_by_waba = {}
        templates_data_by_phone = {}
        for sender in senders:
            if not sender.waba_id:
                templates_data_by_phone[str(sender.id)] = []
                continue
            if sender.waba_id not in templates_by_waba:
                templates = self._templates_for_sender(sender)
                templates_by_waba[sender.waba_id] = waba_utils.serialize_templates_for_frontend(
                    templates,
                    stringify_ids=True,
                )
            templates_data_by_phone[str(sender.id)] = templates_by_waba[sender.waba_id]

        context = {
            "auth_id": data.get("AUTH_ID") or "",
            "refresh_id": data.get("REFRESH_ID") or "",
            "member_id": data.get("member_id") or "",
            "blocks": self.BLOCKS,
            "sender_phones": [{"id": str(item.id), "label": item.phone or str(item.id)} for item in senders],
            "templates_data_by_phone": templates_data_by_phone,
            "recipient_phones": [{"value": f"+{phone}", "label": f"+{phone}"} for phone in recipient_phones],
            "initial_sender_phone_id": str(selected_sender.id) if selected_sender else "",
            "entity_id": entity_id or "",
            "entity_type_code": entity_type_code or "",
        }
        return render(request, "bitrix/placements/waba.html", context)

    def send_template(self, request):
        data = self._request_data(request)
        action = data.get("action")

        try:
            _app, _portal, appinstance = self._resolve_context(data)
        except Exception as exc:
            if action:
                return self._action_response({"ok": False, "error": str(exc)})
            return self._plain_response(str(exc))

        if not appinstance:
            if action:
                return self._action_response({"ok": False, "error": _("appinstance not found")})
            return self._plain_response(_("appinstance not found"))

        sender_phones = self._get_sender_phones(appinstance)
        if action:
            return self._dispatch_action(request, data, appinstance, sender_phones, action)
        return self._render_widget(request, data, appinstance, sender_phones)

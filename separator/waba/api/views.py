import logging
import json
import requests
import redis

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.mixins import CreateModelMixin
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from separator.waba.models import App, PartnerApp, Waba, Phone
from separator.waba import tasks as waba_tasks
from separator.waba.utils import (
    build_embedded_signup_link,
    build_hosted_embedded_signup_link,
    build_popup_embedded_signup_config,
    event_processing,
    get_api_credentials,
    messages_processing,
)

logger = logging.getLogger("waba")
redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


PARTNER_PROXY_ROUTES = {
    "messages": "phone",
    "marketing_messages": "phone",
    "media": "phone",
    "settings": "phone",
    "register": "phone",
    "deregister": "phone",
    "request_code": "phone",
    "verify_code": "phone",
    "call_permissions": "phone",
    "block_users": "phone",
    "calls": "phone",
    "groups": "phone",
    "official_business_account": "phone",
    "whatsapp_business_profile": "phone",
    "message_templates": "waba",
    "phone_numbers": "waba",
    "subscribed_apps": "waba",
    "schedules": "waba",
    "dataset": "waba",
    "events": "dataset",
}


def _resolve_partner_proxy_waba(user, object_id, endpoint=None):
    if not endpoint:
        waba = Waba.objects.select_related("app", "partner_app").filter(
            waba_id=object_id,
            partner_app__owner=user,
            partner_app__active=True,
        ).first()
        if waba:
            return waba, "", None
        phone = Phone.objects.select_related("waba", "waba__app", "waba__partner_app").filter(
            phone_id=object_id,
            waba__partner_app__owner=user,
            waba__partner_app__active=True,
        ).first()
        return (phone.waba if phone else None), "", phone

    edge = (endpoint or "").strip("/").split("/", 1)[0]
    entity = PARTNER_PROXY_ROUTES.get(edge)
    if entity == "phone":
        phone = Phone.objects.select_related("waba", "waba__app", "waba__partner_app").filter(
            phone_id=object_id,
            waba__partner_app__owner=user,
            waba__partner_app__active=True,
        ).first()
        return (phone.waba if phone else None), edge, phone
    if entity == "waba":
        waba = Waba.objects.select_related("app", "partner_app").filter(
            waba_id=object_id,
            partner_app__owner=user,
            partner_app__active=True,
        ).first()
        return waba, edge, None
    if entity == "dataset":
        waba = Waba.objects.select_related("app", "partner_app").filter(
            dataset=object_id,
            partner_app__owner=user,
            partner_app__active=True,
        ).first()
        return waba, edge, None
    return None, edge, None


class WabaWebhook(GenericViewSet, CreateModelMixin):
    queryset = Phone.objects.all()
    authentication_classes = []
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        signature = request.headers.get("X-Hub-Signature-256")
        app_id = request.query_params.get('app_id')
        host = request.get_host()
        
        # Pass raw body for signature verification in the task
        raw_body = request.body.decode('utf-8')

        task = event_processing
        if settings.WABA_EVENTS_SEPARATOR:
            try:
                data = json.loads(raw_body)
                entry = data.get("entry", [{}])[0]
                changes = entry.get("changes", [{}])
                if changes:
                    change = changes[0]
                    field = change.get("field")
                    value = change.get("value", {})
                    if (field == "messages" and value.get("messages")) or field == "smb_message_echoes":
                        task = messages_processing
            except Exception:
                pass

        task.delay(
            raw_body=raw_body, 
            signature=signature, 
            app_id=app_id, 
            host=host
        )
        return HttpResponse("Mark, call me!")

    def list(self, request, *args, **kwargs):
        hub_mode = request.query_params.get("hub.mode")
        hub_challenge = request.query_params.get("hub.challenge")
        hub_verify_token = request.query_params.get("hub.verify_token")

        if hub_mode == "subscribe" and hub_verify_token:
            try:
                app = App.objects.get(
                    verify_token=hub_verify_token,
                    # owner=request.user.id,
                )
                return HttpResponse(hub_challenge, content_type="text/plain")
            except App.DoesNotExist:
                logger.error(
                    f"Verification token not found or does not belong to the user {request.query_params}",
                )
                return HttpResponse(
                    "token not found",
                    status=403,
                    content_type="text/plain",
                )
        return HttpResponse("Bad Request", status=400, content_type="text/plain")


@extend_schema(
    tags=["waba"],
    summary="Proxy partner request to Meta Graph API",
    description=(
        "Proxies partner requests to Meta using the stored WABA access token. "
        "Use the same JSON body as Meta Cloud API, but send it to this endpoint "
        "without Graph API version and without Meta token."
    ),
    request={
        "application/json": {
            "type": "object",
            "additionalProperties": True,
            "description": "Raw JSON payload that will be forwarded to Meta.",
        }
    },
    responses={
        200: OpenApiResponse(description="Raw Meta response."),
        400: OpenApiResponse(description="Meta or proxy error."),
        404: OpenApiResponse(description="Unsupported endpoint or object not found."),
    },
)
@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def partner_graph_proxy(request, object_id, endpoint=""):
    waba, edge, phone = _resolve_partner_proxy_waba(request.user, object_id, endpoint)
    if not waba or not waba.app:
        return Response({"error": "unsupported endpoint or object not found", "edge": edge}, status=404)
    if phone and phone.date_end and phone.date_end <= timezone.now():
        return Response({"error": "phone tariff expired"}, status=402)

    endpoint = (endpoint or "").strip("/")
    graph_path = f"{object_id}/{endpoint}" if endpoint else object_id
    meta_app, access_token = get_api_credentials(waba=waba)
    graph_url = f"{settings.FACEBOOK_API_URL}/v{meta_app.api_version}.0/{graph_path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    content_type = request.headers.get("Content-Type")
    if content_type:
        headers["Content-Type"] = content_type
    accept = request.headers.get("Accept")
    if accept:
        headers["Accept"] = accept

    query_params = request.query_params.copy()
    query_params.pop("api-key", None)

    meta_response = requests.request(
        request.method,
        graph_url,
        params=query_params,
        data=request._request.body if request.method in {"POST", "DELETE"} else None,
        headers=headers,
    )
    response = HttpResponse(
        meta_response.content,
        status=meta_response.status_code,
        content_type=meta_response.headers.get("Content-Type", "application/json"),
    )
    return response


@extend_schema(
    tags=["waba"],
    summary="Create WhatsApp Embedded Signup authorization link",
    description=(
        "Returns a Facebook Embedded Signup URL. Auth is required via session, "
        "`Authorization: Token <token>`, or `?api-key=<token>`. "
        "For partner onboarding pass `partner_app_id` as a query parameter for GET "
        "or in the JSON body for POST."
    ),
    parameters=[
        OpenApiParameter(
            name="partner_app_id",
            location=OpenApiParameter.QUERY,
            required=False,
            type=str,
            description="UUID of an active partner app owned by the authenticated integrator.",
        ),
        OpenApiParameter(
            name="api-key",
            location=OpenApiParameter.QUERY,
            required=False,
            type=str,
            description="DRF token alternative to the Authorization header.",
        ),
    ],
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "partner_app_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of an active partner app owned by the authenticated integrator.",
                }
            },
        }
    },
    responses={
        200: OpenApiResponse(
            response={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "format": "uri",
                    }
                },
            },
            description="Facebook authorization URL.",
        ),
        403: OpenApiResponse(description="Authenticated user is not an integrator."),
        404: OpenApiResponse(description="Partner app or WABA app was not found."),
    },
    examples=[
        OpenApiExample(
            "Partner GET",
            value={"url": "https://www.facebook.com/v25.0/dialog/oauth?..."},
            response_only=True,
        ),
        OpenApiExample(
            "Partner POST body",
            value={"partner_app_id": "00000000-0000-0000-0000-000000000000"},
            request_only=True,
        ),
    ],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def embedded_signup_link(request):
    partner_app = None
    partner_app_id = request.query_params.get("partner_app_id") or request.data.get("partner_app_id")
    if partner_app_id:
        if not getattr(request.user, "integrator", False):
            return Response({"error": "permission denied"}, status=403)
        partner_app = PartnerApp.objects.select_related("app").filter(
            id=partner_app_id,
            owner=request.user,
            active=True,
        ).first()
        if not partner_app:
            return Response({"error": "Partner app not found"}, status=404)

    try:
        domain = request.get_host().split(':')[0]
        app = partner_app.app if partner_app else App.objects.filter(sites__domain__iexact=domain).first()
        if not app:
            raise App.DoesNotExist

        if partner_app:
            url = build_embedded_signup_link(request, request.user, partner_app=partner_app)
            return Response({"url": url})

        if app.auth_flow == App.AuthFlow.HOSTED:
            return Response({
                "flow": app.auth_flow,
                "url": build_hosted_embedded_signup_link(app),
            })
        if app.auth_flow == App.AuthFlow.POPUP:
            data = build_popup_embedded_signup_config(request, request.user, partner_app=partner_app)
            data["flow"] = app.auth_flow
            return Response(data)

        url = build_embedded_signup_link(request, request.user, partner_app=partner_app)
    except App.DoesNotExist:
        domain = request.get_host().split(':')[0]
        return Response({"error": f"App not found for domain {domain}"}, status=404)
    except Exception as e:
        return Response({"error": str(e)}, status=500)
    return Response({"flow": App.AuthFlow.MANUAL, "url": url})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def embedded_signup_popup_message(request):
    message = request.data.get("message") or _("Embedded Signup was cancelled.")
    messages.error(request, message)
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def embedded_signup_popup_callback(request):
    request_id = request.data.get("request_id")
    code = request.data.get("code")
    session = request.data.get("session") or {}
    logger.info(
        "Popup callback received: request_id=%s user_id=%s has_code=%s waba_id=%s phone_number_id=%s business_id=%s",
        request_id,
        getattr(request.user, "id", None),
        bool(code),
        session.get("waba_id"),
        session.get("phone_number_id"),
        session.get("business_id"),
    )
    if not request_id:
        return Response({"error": "Request ID is missing"}, status=400)
    if not code:
        return Response({"error": "Authorization code is missing"}, status=400)
    if not session.get("waba_id") or not session.get("phone_number_id"):
        return Response({"error": "Popup session data is missing"}, status=400)

    existing = redis_client.json().get(request_id)
    if not existing:
        return Response({"error": "Request data is missing"}, status=404)
    if str(existing.get("user")) != str(request.user.id):
        return Response({"error": "permission denied"}, status=403)

    app_id = existing.get("app")
    app = App.objects.filter(client_id=app_id, auth_flow=App.AuthFlow.POPUP).first()
    if not app:
        return Response({"error": "Popup app not found"}, status=404)

    state_lock_key = f"{request_id}:used"
    if not redis_client.set(state_lock_key, "1", nx=True, ex=7200):
        return Response({"error": "Request has already been used"}, status=400)

    redis_client.json().set(request_id, "$.code", code)
    redis_client.json().set(request_id, "$.popup_session", session)
    waba_tasks.add_popup_phone.delay(request_id, app_id)
    messages.success(request, _('The number has been successfully added. It will appear here in a few minutes.'))

    logger.info(
        "Popup callback scheduled: request_id=%s user_id=%s waba_id=%s phone_number_id=%s",
        request_id,
        getattr(request.user, "id", None),
        session.get("waba_id"),
        session.get("phone_number_id"),
    )
    return Response({
        "ok": True,
        "scheduled": True,
        "waba_id": session.get("waba_id"),
        "phone_number_id": session.get("phone_number_id"),
    })

import uuid
import requests
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from rest_framework.authtoken.models import Token

from .crest import call_method
from .utils import process_placement
from .forms import BitrixPortalForm
from .forms import VerificationCodeForm
from .models import AppInstance, Bitrix, VerificationCode, Line, App

from thoth.users.models import Message


def link_portal(request, code):
    try:
        uuid_code = uuid.UUID(code)
        verification = VerificationCode.objects.get(code=uuid_code)
        portal = verification.portal
        user = request.user

        if not portal:
            messages.error(request, "Портал по коду не найден.")
            return

        if verification.is_valid():
            portal.owner = user
            portal.save()
            AppInstance.objects.filter(portal=portal).update(owner=user)
            Line.objects.filter(portal=portal).update(owner=user)
            verification.delete()
            messages.success(request, "Портал и связанные приложения успешно закреплены за вами.")
        else:
            messages.error(request, "Код подтверждения истек.")
    except (VerificationCode.DoesNotExist, ValueError):
        messages.error(request, "Неверный код подтверждения.")


@login_required
def portals(request):
    user_portals = Bitrix.objects.filter(owner=request.user)
    portal_form = BitrixPortalForm()
    verification_form = VerificationCodeForm()

    if request.method == "POST":
        if "send_code" in request.POST:
            portal_form = BitrixPortalForm(request.POST)
            if portal_form.is_valid():
                portal_address = portal_form.cleaned_data["portal_address"]
                try:
                    portal = Bitrix.objects.get(domain=portal_address, owner=None)
                    verification = VerificationCode.objects.filter(portal=portal).first()

                    if verification and verification.is_valid():
                        code = verification.code
                    else:
                        code = uuid.uuid4()
                        if verification:
                            verification.code = code
                            verification.expires_at = timezone.now() + timedelta(days=1)
                            verification.save()
                        else:
                            VerificationCode.objects.create(
                                portal=portal,
                                code=code,
                                expires_at=timezone.now() + timedelta(days=1),
                            )

                    appinstance = AppInstance.objects.filter(portal=portal).first()

                    payload = {
                        "message": f"Для привязки портала перейдите по ссылке https://{appinstance.app.site}/portals/?code={code}",
                        "USER_ID": appinstance.portal.user_id,
                    }

                    call_method(appinstance, "im.notify.system.add", payload)

                    messages.success(
                        request, "Код подтверждения отправлен на ваш портал Bitrix24."
                    )
                except Bitrix.DoesNotExist:
                    messages.error(request, "Портал не найден или уже закреплен за другим пользователем.")
        
        elif "confirm" in request.POST:
            verification_form = VerificationCodeForm(request.POST)
            if verification_form.is_valid():
                code = verification_form.cleaned_data["confirmation_code"]
                link_portal(request, code)

    elif request.method == "GET":
        code = request.GET.get("code")
        if code:
            link_portal(request, code)

    message = Message.objects.filter(code="bitrix").first()

    return render(
        request,
        "bitrix24.html",
        {
            "user_portals": user_portals,
            "portal_form": portal_form,
            "verification_form": verification_form,
            "message": message,
        },
    )


@login_required
def link_user(request):
    member_id = request.session.get("member_id")
    app_url = request.session.get("app_url")
    if not member_id:
        return HttpResponseForbidden("403 Forbidden")
    try:
        portal = Bitrix.objects.get(member_id=member_id)
    except Bitrix.DoesNotExist:
        return redirect("portals")
    except Exception as e:
        return HttpResponse(f"Error: {e}", status=500)

    if portal.owner is None:
        portal.owner = request.user
        portal.save()

    AppInstance.objects.filter(portal=portal, owner__isnull=True).update(owner=request.user)
    Line.objects.filter(portal=portal, owner__isnull=True).update(owner=request.user)

    request.session.pop("member_id", None)
    if app_url:
        return redirect(app_url)
    else:
        return redirect("portals")


@csrf_exempt
def app_install(request):
    if request.method == "HEAD":
        return HttpResponse("ok")

    app_id = request.GET.get("app-id")
    protocol = request.GET.get("PROTOCOL")
    domain = request.GET.get("DOMAIN")
    data = request.POST

    member_id = data.get("member_id")
    auth_id = data.get("AUTH_ID")

    if not app_id or not member_id or not domain or not auth_id:
        return HttpResponseForbidden("Missing parameters")

    try:
        app = App.objects.get(id=app_id)
    except App.DoesNotExist:
        return HttpResponseForbidden("403 Forbidden: app not found")

    proto = "https" if protocol == "1" else "http"

    try:
        response = requests.get(f"https://oauth.bitrix24.tech/rest/app.info", params={"auth": auth_id})
        response.raise_for_status()
        user_id = response.json().get("result").get("user_id")
    except requests.RequestException as e:
        user_id = None

    portal, _ = Bitrix.objects.get_or_create(
        member_id=member_id,
        defaults={
            "user_id": user_id,
            "domain": domain,
            "protocol": proto,
        }
    )

    api_key, _ = Token.objects.get_or_create(user=app.owner)

    payload = {
        "event": "ONAPPINSTALL",
        "HANDLER": f"https://{app.site}/api/bitrix/?api-key={api_key.key}&app-id={app_id}",
        "auth": auth_id,
    }

    try:
        response = requests.post(f"{proto}://{domain}/rest/event.bind", json=payload)
        response.raise_for_status()
    except requests.RequestException as e:
        resp = response.json()
        error_description = resp.get("error_description")
        if "Handler already binded" in error_description:
            return render(request, "install_finish.html")
        else:
            return HttpResponse(f"Bitrix event.bind failed {response.status_code, resp}")

    return render(request, "install_finish.html")


@csrf_exempt
def app_settings(request):
    if request.method == "POST":
        try:
            app_id = request.GET.get("app-id")
            data = request.POST
            domain = request.GET.get("DOMAIN")
            member_id = data.get("member_id")
            portal = Bitrix.objects.get(domain=domain, member_id=member_id)
        except Bitrix.DoesNotExist:
            return redirect("portals")
        except Exception as e:
            return HttpResponseForbidden(f"403 Forbidden")        

        placement = data.get("PLACEMENT")
        if placement == "SETTING_CONNECTOR":
            return process_placement(request)
        elif placement == "DEFAULT":
            try:
                app = App.objects.get(id=app_id)
            except App.DoesNotExist:
                return HttpResponseForbidden("403 Forbidden: app not found")
            if not portal.owner:
                request.session["member_id"] = member_id
                request.session["app_url"] = app.page_url
                return redirect("link_user")
            else:
                if request.user.is_authenticated:
                    AppInstance.objects.filter(portal=portal, owner__isnull=True).update(owner=request.user)
                return redirect(app.page_url)
        else:
            return redirect("portals")
    elif request.method == "HEAD":
        return HttpResponse("ok")
    elif request.method == "GET":
        return redirect("portals")
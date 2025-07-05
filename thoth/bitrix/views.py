import uuid
import requests
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
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

from thoth.decorators import login_message_required
from thoth.users.tasks import create_user_task

from django.contrib.auth import get_user_model, login, logout
User = get_user_model()

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


@login_message_required(code="bitrix")
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

    return render(
        request,
        "bitrix24.html",
        {
            "user_portals": user_portals,
            "portal_form": portal_form,
            "verification_form": verification_form,
        },
    )


def get_owner(request):
    protocol = request.GET.get("PROTOCOL")
    domain = request.GET.get("DOMAIN")
    data = request.POST

    member_id = data.get("member_id")
    auth_id = data.get("AUTH_ID")
    refresh_id = data.get("REFRESH_ID")
    proto = "https" if protocol == "1" else "http"

    try:
        response = requests.get(f"https://oauth.bitrix24.tech/rest/app.info", params={"auth": auth_id})
        response.raise_for_status()
        app_data = response.json().get("result")
        user_id = app_data.get("user_id")
        client_id = app_data.get("client_id")
    except requests.RequestException:
        return None
    
    portal, created = Bitrix.objects.get_or_create(
        member_id=member_id,
        defaults={
            "user_id": user_id,
            "domain": domain,
            "protocol": proto,
        }
    )
    
    try:
        app = App.objects.get(client_id=client_id)
    except Exception as e:
        return None
    
    try:
        profile = requests.post(f"{proto}://{domain}/rest/profile", json={"auth": auth_id})
        profile_data = profile.json().get("result")
        is_admin = profile_data.get("ADMIN")
    except Exception as e:
        return None

    app_instance = AppInstance.objects.filter(portal=portal, app=app).first()
    if app_instance and is_admin:
        app_instance.access_token = auth_id
        app_instance.refresh_token = refresh_id
        app_instance.save()

    if int(portal.user_id) != int(user_id):
        return None

    if portal.owner:
        return portal.owner
    else:
        if request.user.is_authenticated:
            portal.owner = request.user
            portal.save()
            return request.user
        try:
            user_data = requests.post(f"{proto}://{domain}/rest/user.current", json={"auth": auth_id})
            user_data.raise_for_status()
            user_data = user_data.json().get("result")
            user_name = user_data.get("NAME")
            user_last_name = user_data.get("LAST_NAME")
            user_email = user_data.get("EMAIL")
            user_phone = user_data.get("PERSONAL_MOBILE") or user_data.get("WORK_PHONE")
            user, created = User.objects.get_or_create(
                email=user_email,
                defaults={
                    "name": f"{user_name} {user_last_name}".strip(),
                    "first_name": user_name,
                    "last_name": user_last_name,
                    "phone_number": user_phone,
                }
            )
            portal.owner = user
            portal.save()

            if user_email and settings.CHATWOOT_ENABLED:
                from django.db import transaction
                def run_task():
                    create_user_task.delay(user_email, user.id)
                transaction.on_commit(run_task)

            return user
        except Exception as e:
            print("Error", e)
            return None


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
        return redirect("portals")

    try:
        app = App.objects.get(id=app_id)
    except App.DoesNotExist:
        return redirect("portals")

    proto = "https" if protocol == "1" else "http"
    owner = get_owner(request)
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
        except Exception as e:
            return redirect("portals")

        placement = data.get("PLACEMENT")
        if placement == "SETTING_CONNECTOR":
            return process_placement(request)
        
        elif placement == "DEFAULT":
            try:
                app = App.objects.get(id=app_id)
            except Exception:
                return redirect("portals")
            app_url = app.page_url
            owner = get_owner(request)
            
            if owner is None:
                logout(request)
                return redirect(app_url)
            
            should_login = not request.user.is_authenticated or request.user != owner
            if should_login:
                if request.user.is_authenticated:
                    logout(request)
                try:
                    login(request, owner, backend='django.contrib.auth.backends.ModelBackend')
                except Exception:
                    return redirect(app_url)

            AppInstance.objects.filter(portal=portal, owner__isnull=True).update(owner=owner)
            Line.objects.filter(portal=portal, owner__isnull=True).update(owner=owner)
            return redirect(f"{app_url}?domain={domain}")
        else:
            return redirect("portals")
    elif request.method == "HEAD":
        return HttpResponse("ok")
    elif request.method == "GET":
        return redirect("portals")
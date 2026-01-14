import os
import uuid
import requests
from datetime import timedelta

from django.db.models import Q
from django.conf import settings
from django.contrib import messages
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import HttpResponse, FileResponse, Http404, HttpResponseForbidden
from django.core.signing import TimestampSigner, SignatureExpired, BadSignature
from django.views.decorators.csrf import csrf_exempt
from django.utils.translation import gettext as _

from .crest import call_method
from .tasks import call_api, prepare_lead
from .utils import process_placement, get_b24_user, get_instances, get_app
from .forms import BitrixPortalForm, VerificationCodeForm
from .models import AppInstance, Bitrix, VerificationCode, Line
from .models import User as B24_user

from separator.decorators import login_message_required

from django.contrib.auth import get_user_model, login, logout
User = get_user_model()


def link_ojects(portal: Bitrix, user):
    if not portal.owner:
        portal.owner = user
        portal.save()
    AppInstance.objects.filter(portal=portal, owner__isnull=True).update(owner=user)
    Line.objects.filter(portal=portal, owner__isnull=True).update(owner=user)
    B24_user.objects.filter(bitrix=portal, owner__isnull=True).update(owner=user)


def link_portal(request, code):
    try:
        uuid_code = uuid.UUID(code)
        verification = VerificationCode.objects.get(code=uuid_code)
        portal = verification.portal
        user = request.user

        if not portal:
            messages.error(request, _("Portal not found by code."))
            return

        if verification.is_valid():
            verification.delete()
            link_ojects(portal, user)
            messages.success(request, _("Portal and related apps successfully linked to you."))
        else:
            messages.error(request, _("Verification code expired."))
    except (VerificationCode.DoesNotExist, ValueError):
        messages.error(request, _("Invalid verification code."))


@login_message_required(code="bitrix")
def portals(request):
    b24_data = request.session.get('b24_data', None)
    page_url = request.session.pop('page_url', None)
    if b24_data and page_url:
        try:
            member_id = b24_data.get("member_id")
            portal = Bitrix.objects.get(member_id=member_id)
            link_ojects(portal, request.user)
        except Bitrix.DoesNotExist:
            pass
        return redirect(page_url)
    user_portals = Bitrix.objects.filter(
        Q(users__owner=request.user) | Q(owner=request.user)
    ).distinct()
    portal_form = BitrixPortalForm()
    verification_form = VerificationCodeForm()
    b24_admin = B24_user.objects.filter(owner=request.user, admin=True).first()

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
                        "message": _("Для привязки портала перейдите по ссылке https://{site}/portals/?code={code}").format(site=appinstance.app.site, code=code),
                        "USER_ID": b24_admin.user_id,
                    }

                    call_method(appinstance, "im.notify.system.add", payload)

                    messages.success(
                        request, _("Код подтверждения отправлен на ваш портал Bitrix24.")
                    )
                except Bitrix.DoesNotExist:
                    messages.error(request, _("Портал не найден или уже закреплен за другим пользователем."))
        
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
        "bitrix/portals.html",
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
        app = get_app(auth_id)
        if not app.autologin:
            return None
    except Exception as e:
        return None
    
    portal, created = Bitrix.objects.get_or_create(
        member_id=member_id,
        defaults={
            "domain": domain,
            "protocol": proto,
        }
    )

    try:
        b24_user = get_b24_user(app, portal, auth_id, refresh_id)
    except Exception as e:
        raise
    
    if b24_user.owner:
        owner_user = b24_user.owner
    else:
        if request.user.is_authenticated:
            owner_user = request.user
        else:
            try:
                user_data = requests.post(f"{proto}://{domain}/rest/user.current", json={"auth": auth_id})
                user_data.raise_for_status()
                user_data = user_data.json().get("result")
                user_name = user_data.get("NAME")
                user_last_name = user_data.get("LAST_NAME")
                user_email = user_data.get("EMAIL")
                user_phone = user_data.get("PERSONAL_MOBILE") or user_data.get("WORK_PHONE")

                from separator.users.tasks import get_site
                
                owner_user, created = User.objects.get_or_create(
                    email=user_email,
                    defaults={
                        "name": f"{user_name} {user_last_name}".strip(),
                        "first_name": user_name,
                        "last_name": user_last_name,
                        "phone_number": user_phone,
                        "site": get_site(request)
                    }
                )

            except Exception as e:
                print("Error", e)
                return None
        b24_user.owner = owner_user
        b24_user.save()

    if not portal.owner:
        portal.owner = owner_user
        portal.save()

    return owner_user


@csrf_exempt
def app_install(request):
    if request.method == "HEAD":
        return HttpResponse("ok")

    protocol = request.GET.get("PROTOCOL")
    domain = request.GET.get("DOMAIN")
    data = request.POST
    auth_id = data.get("AUTH_ID")

    try:
        app = get_app(auth_id)
    except Exception as e:
        messages.error(request, e)
        return redirect("/")

    try:
        user = get_owner(request)
    except Exception as e:
        messages.error(request, e)
        return redirect("/")

    payload = {
        "event": "ONAPPINSTALL",
        "HANDLER": f"https://{app.site}/api/bitrix/",
        "auth": auth_id,
    }

    request.session["installed_app"] = app.name
    try:
        proto = "https" if protocol == "1" else "http"
        response = requests.post(f"{proto}://{domain}/rest/event.bind", json=payload, timeout=25)
        response.raise_for_status()
    except requests.RequestException as e:
        # Check if it's a specific API error response
        if e.response is not None:
            try:
                resp = e.response.json()
                error_description = resp.get("error_description", "")
                if "Handler already binded" in error_description:
                    return render(request, "bitrix/install_finish.html")
                error_detail = f"Status: {e.response.status_code}, Response: {resp}"
            except ValueError:
                error_detail = f"Status: {e.response.status_code}, Body: {e.response.text[:200]}"
        else:
            # Connection errors, Timeouts, etc.
            error_detail = str(e)

        messages.error(request, f"Installation failed. Error connecting to Bitrix ({domain}): {error_detail}")
        return redirect("/")

    return render(request, "bitrix/install_finish.html")


@csrf_exempt
def app_settings(request):
    if request.method == "POST":
        data = request.POST
        domain = request.GET.get("DOMAIN")        
        auth_id = data.get("AUTH_ID")
        try:
            app = get_app(auth_id)
        except Exception as e:
            messages.error(request, e)
            return redirect("/")
        member_id = data.get("member_id")
        try:
            portal = Bitrix.objects.filter(member_id=member_id).first()
            # if portal.domain != domain:
            #     portal.domain = domain
            #     portal.save()
        except Exception as e:
            pass

        placement = data.get("PLACEMENT")
        request.session['b24_data'] = request.POST.dict()
        request.session['page_url'] = app.page_url
        if placement == "SETTING_CONNECTOR":
            return process_placement(request)
        
        elif placement == "DEFAULT":
            installed_app = None
            try:
                installed_app = request.session.pop("installed_app")
            except Exception:
                pass
            app_url = app.page_url
            try:
                user = get_owner(request)
            except Exception as e:
                messages.error(request, e)
                return redirect("/")
            
            if user and portal:
                link_ojects(portal, user)
            
            should_login = not request.user.is_authenticated or request.user != user
            if should_login and app.autologin:
                if request.user.is_authenticated:
                    logout(request)
                try:
                    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                except Exception:
                    return redirect(app_url)
            if installed_app:
                if user.phone_number and not user.integrator:
                    prepare_lead.delay(user.id, f"App installed: {app.name}")
                else:
                    request.session["installed_app"] = installed_app
            return render(request, "bitrix/cookie_test.html", {"app_url": app_url})
        else:
            return portals(request)
    elif request.method == "HEAD":
        return HttpResponse("ok")
    elif request.method == "GET":
        return portals(request)


@login_required
def portal_detail(request, portal_id):
    """Отображение и редактирование данных портала"""
    portals, lines = get_instances(request)
    b24_user = B24_user.objects.filter(owner=request.user, bitrix__id=portal_id).first()
    portal = portals.filter(id=portal_id).first()
    lines = lines.filter(portal=portal)
    
    if request.method == 'POST':
        if b24_user and b24_user.admin:
            imopenlines_auto_finish = request.POST.get('imopenlines_auto_finish') == 'on'
            portal.finish_delay = int(request.POST.get('finish_delay'))
            portal.imopenlines_auto_finish = imopenlines_auto_finish
            portal.save()
            for line in lines:
                if request.POST.get(f"delete_line_{line.id}") == 'on':
                    call_api.delay(line.app_instance.id, "imopenlines.config.delete", {"CONFIG_ID": line.line_id})
                    line.delete()
                    continue
                new_name = request.POST.get(f"line_name_{line.id}")
                if new_name is not None and new_name != line.name:
                    line.name = new_name
                    line.save()
                    if line.app_instance:
                        payload = {
                            "CONFIG_ID": line.line_id,
                            "PARAMS": {
                                "LINE_NAME": new_name
                            }
                        }
                        call_api.delay(line.app_instance.id, "imopenlines.config.update", payload)
            
            if imopenlines_auto_finish:
                # Если включили автозакрытие - получаем первый подходящий инстанс и отправляем event.bind
                instance = AppInstance.objects.filter(portal=portal, app__imopenlines_auto_finish=True).first()
                if instance:
                    payload = {
                        "event": "ONCRMDEALUPDATE",
                        "HANDLER": f"https://{instance.app.site}/api/bitrix/",
                    }
                    call_api.delay(instance.id, "event.bind", payload)
                    messages.success(request, _('Auto-close chats is enabled and the event is binded'))
                else:
                    messages.error(request, _("You don't have an app with auto-close chats."))
            else:
                # Если отключили автозакрытие - получаем все инстансы и отправляем event.unbind
                instances = AppInstance.objects.filter(portal=portal, app__imopenlines_auto_finish=True)
                for instance in instances:
                    payload = {
                        "event": "ONCRMDEALUPDATE",
                        "HANDLER": f"https://{instance.app.site}/api/bitrix/",
                    }
                    call_api.delay(instance.id, "event.unbind", payload)
                messages.success(request, _('Auto-closing of chats is disabled and events are unbind'))
        else:
            messages.error(request, _('Administrator rights are required to edit the portal.'))
        
        return redirect('portal_detail', portal_id=portal_id)
    
    return render(request, 
                  'bitrix/portal_detail.html', 
                  {
                      'portal': portal,
                      'open_lines': lines
                      })


def log_and_serve_temp_file(request, path):
    # Verify signature
    signer = TimestampSigner()
    ttl = getattr(settings, 'BITRIX_TEMP_FILE_TTL', 1800)
    try:
        # Validate signature and check if it's within TTL
        original_filename = signer.unsign(path, max_age=ttl)
    except SignatureExpired:
        return HttpResponseForbidden("Link expired")
    except BadSignature:
        return HttpResponseForbidden("Invalid signature")

    file_path = os.path.join(settings.MEDIA_ROOT, 'temp', original_filename)
    
    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'))
    else:
        raise Http404("File does not exist")

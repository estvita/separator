import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt

from .crest import call_method
from .utils import process_placement
from .forms import BitrixPortalForm
from .forms import VerificationCodeForm
from .models import AppInstance, Bitrix, VerificationCode, Line

from thoth.users.models import Message


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
                        "message": f"Ваш код подтверждения: {code}",
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
                confirmation_code = verification_form.cleaned_data["confirmation_code"]
                try:
                    # Попытка преобразования кода в UUID
                    uuid_code = uuid.UUID(confirmation_code)
                    verification = VerificationCode.objects.get(code=uuid_code)
                    portal = verification.portal

                    if verification.is_valid():
                        portal.owner = request.user
                        portal.save()
                        AppInstance.objects.filter(portal=portal).update(owner=request.user)
                        Line.objects.filter(portal=portal).update(owner=request.user)
                        verification.delete()
                        messages.success(request, "Портал и связанные приложения успешно закреплены за вами.")
                    else:
                        messages.error(request, "Код подтверждения истек.")
                except (VerificationCode.DoesNotExist, ValueError):
                    messages.error(request, "Неверный код подтверждения.")

    else:
        portal_form = BitrixPortalForm()
        verification_form = VerificationCodeForm()

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
    return redirect("portals")


@csrf_exempt
def app_settings(request):
    if request.method == "POST":
        try:
            data = request.POST
            domain = request.GET.get("DOMAIN")
            member_id = data.get("member_id")
            Bitrix.objects.get(domain=domain, member_id=member_id)
        except Bitrix.DoesNotExist:
            return HttpResponseForbidden("403 Forbidden")
        except Exception as e:
            return HttpResponseForbidden(f"403 Forbidden")

        placement = data.get("PLACEMENT")
        if placement == "SETTING_CONNECTOR":
            return process_placement(request)
        elif placement == "DEFAULT":
            request.session["member_id"] = member_id
            return redirect("link_user")
        else:
            return redirect("portals")
    elif request.method == "HEAD":
        return HttpResponse("ok")
    elif request.method == "GET":
        return redirect("portals")
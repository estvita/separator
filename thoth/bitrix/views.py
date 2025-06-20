import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .crest import call_method
from .utils import process_placement
from .forms import BitrixPortalForm
from .forms import VerificationCodeForm
from .models import AppInstance, Bitrix, VerificationCode, Line


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
                        code = verification.code  # Используем существующий код
                    else:
                        code = uuid.uuid4()  # Генерируем новый код
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

    return render(
        request,
        "bitrix24.html",
        {
            "user_portals": user_portals,
            "portal_form": portal_form,
            "verification_form": verification_form,
        },
    )


@login_required
def link_user(request):
    member_id = request.session.get("member_id")
    if not member_id:
        return HttpResponse("member_id not found in session")
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
        data = request.POST
        placement = data.get("PLACEMENT")
        if placement == "SETTING_CONNECTOR":
            return process_placement(request)
        try:
            member_id = request.POST.get("member_id")
            if not member_id:
                return HttpResponse("Missing member_id")
            request.session["member_id"] = member_id
            return redirect("link_user")
        except Exception as e:
            return HttpResponse(f"Error: {e}", status=500)
        
    elif request.method == "HEAD":
        return HttpResponse("ok")
    
    elif request.method == "GET":
        return redirect("portals")
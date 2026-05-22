from django.db.models import Q
from django.contrib import messages
from django.shortcuts import render, get_object_or_404, redirect
from django.utils.dateparse import parse_time
from django.utils import timezone
import requests

from separator.decorators import login_message_required, user_message

import separator.bitrix.utils as bitrix_utils

from .forms import OlxAdvertForm, olx_advert_initial
from .models import OlxAdvert, OlxApp, OlxCategory, OlxCity, OlxDistrict, OlxRegion, OlxUser
from .tasks import refresh_token


def _olx_headers(olx_user):
    return {
        "Authorization": f"Bearer {olx_user.access_token}",
        "Version": "2.0",
    }


def _olx_request(olx_user, method, path, **kwargs):
    base_url = f"https://www.{olx_user.olxapp.client_domain}/api/partner"
    response = requests.request(method, f"{base_url}{path}", headers=_olx_headers(olx_user), **kwargs)
    if response.status_code == 401:
        refresh_token(olx_user.olx_id)
        olx_user.refresh_from_db(fields=["access_token"])
        response = requests.request(method, f"{base_url}{path}", headers=_olx_headers(olx_user), **kwargs)
    return response


def _upsert_advert(olx_user, advert_data):
    client_domain = olx_user.olxapp.client_domain
    location = advert_data.get("location") or {}
    category = OlxCategory.objects.filter(
        client_domain=client_domain,
        olx_id=advert_data.get("category_id"),
    ).first()
    city = OlxCity.objects.filter(
        client_domain=client_domain,
        olx_id=location.get("city_id"),
    ).first()
    district = OlxDistrict.objects.filter(
        client_domain=client_domain,
        olx_id=location.get("district_id"),
    ).first()

    advert, _ = OlxAdvert.objects.update_or_create(
        olx_user=olx_user,
        advert_id=advert_data["id"],
        defaults={
            "title": advert_data.get("title") or "",
            "status": advert_data.get("status") or "",
            "url": advert_data.get("url"),
            "category": category,
            "city": city,
            "district": district,
            "payload": advert_data,
        },
    )
    return advert


def _sync_account_adverts(olx_user):
    response = _olx_request(olx_user, "GET", "/adverts", params={"limit": 100})
    if response.status_code != 200:
        return response

    for advert_data in response.json().get("data", []):
        _upsert_advert(olx_user, advert_data)
    return response


def _advert_data(response):
    payload = response.json()
    if isinstance(payload, dict):
        return payload.get("data", payload)
    return {}


def _olx_error_message(response):
    try:
        payload = response.json()
    except ValueError:
        return response.text

    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return response.text

    validation = error.get("validation") or []
    if validation:
        parts = [
            item.get("detail") or item.get("title") or item.get("field")
            for item in validation
            if isinstance(item, dict)
        ]
        parts = [part for part in parts if part]
        if parts:
            return "; ".join(parts)

    return error.get("detail") or error.get("title") or response.text


def _get_allowed_olx_user(request, account_id):
    portals, _, _ = bitrix_utils.get_instances(request, "olx")
    return get_object_or_404(
        OlxUser,
        Q(id=account_id),
        Q(line__portal__in=portals) | Q(owner=request.user),
    )


def _has_active_subscription(olx_user):
    return not olx_user.date_end or olx_user.date_end > timezone.now()


@login_message_required(code="olx")
def olx_accounts(request):
    connector_service = "olx"
    portals, instances, lines = bitrix_utils.get_instances(request, connector_service)
    if not instances:
        user_message(request, "olx_install")

    b24_data = request.session.get('b24_data')
    selected_portal = None
    if b24_data:
        member_id = b24_data.get("member_id")
        if member_id:
            selected_portal = portals.filter(member_id=member_id).first()
    if selected_portal:
        accounts = OlxUser.objects.filter(
            Q(line__portal=selected_portal) | Q(owner=request.user, line__isnull=True)
        )
        lines = lines.filter(portal=selected_portal)
        instances = instances.filter(portal=selected_portal)
    else:
        accounts = OlxUser.objects.filter(
            Q(line__portal__in=portals) | Q(owner=request.user)
        )

    olx_apps = OlxApp.objects.all()

    if request.method == "POST":
        if "filter_portal_id" in request.POST:
            filter_portal_id = request.POST.get("filter_portal_id")
            if filter_portal_id == "all":
                request.session.pop('b24_data', None)
            else:
                portal = portals.filter(id=filter_portal_id).first()
                if portal:
                    request.session['b24_data'] = {"member_id": portal.member_id}
            return redirect('olx-accounts')
        action = request.POST.get("action")

        if action == "connect":
            olx_app_id = request.POST.get("olx_app")
            olx_app = OlxApp.objects.get(id=olx_app_id)
            return render(request, "olx/redirect_page.html", {"auth_link": olx_app.authorization_link})
        else:
            olx_id = request.POST.get("olx_id")
            line_id = request.POST.get("line_id")
            olx_user = get_object_or_404(OlxUser, id=olx_id)
            if olx_user.owner_id and olx_user.owner_id != request.user.id:
                messages.error(request, "This number is linked to another user")
                return redirect("olx-accounts")
            try:
                bitrix_utils.connect_line(request, line_id, olx_user, connector_service)
            except Exception as e:
                messages.error(request, str(e))

    return render(request, "olx/accounts.html", 
        {
            "olx_accounts": accounts,
            "olx_apps": olx_apps,
            "instances": instances,
            "olx_lines": lines,
            "portals": portals,
            "selected_portal_id": selected_portal.id if selected_portal else "all",
        }
    )


@login_message_required(code="olx")
def olx_account_adverts(request, account_id):
    olx_user = _get_allowed_olx_user(request, account_id)
    if not _has_active_subscription(olx_user):
        messages.error(request, "OLX subscription has expired.")
        return redirect("olx-accounts")

    client_domain = olx_user.olxapp.client_domain
    edit_advert = None
    copy_advert = None

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "sync_adverts":
            response = _sync_account_adverts(olx_user)
            if response.status_code == 200:
                messages.success(request, "OLX adverts synchronized.")
            else:
                messages.error(request, f"OLX adverts sync failed: {_olx_error_message(response)}")
            return redirect("olx-account-adverts", account_id=olx_user.id)
        if action in {"create_advert", "update_advert"}:
            if action == "update_advert":
                edit_advert = get_object_or_404(OlxAdvert, id=request.POST.get("advert_id"), olx_user=olx_user)
            form = OlxAdvertForm(request.POST, client_domain=client_domain)
            if form.is_valid():
                if action == "create_advert":
                    response = _olx_request(olx_user, "POST", "/adverts", json=form.build_payload())
                else:
                    response = _olx_request(
                        olx_user,
                        "PUT",
                        f"/adverts/{edit_advert.advert_id}",
                        json=form.build_payload(),
                    )
                if response.status_code == 200:
                    advert_data = _advert_data(response)
                    advert = _upsert_advert(olx_user, advert_data)
                    days = form.cleaned_data["pushup_interval_days"]
                    advert.pushup_interval_days = days
                    advert.pushup_enabled = days > 0
                    advert.pushup_time = form.cleaned_data.get("pushup_time") or advert.pushup_time
                    advert.next_pushup_at = advert.calculate_next_pushup_at()
                    advert.save(
                        update_fields=[
                            "pushup_interval_days",
                            "pushup_enabled",
                            "pushup_time",
                            "next_pushup_at",
                        ],
                    )
                    if action == "create_advert":
                        messages.success(request, "OLX advert created.")
                    else:
                        messages.success(request, "OLX advert updated.")
                    return redirect("olx-account-adverts", account_id=olx_user.id)
                messages.error(request, f"OLX advert save failed: {_olx_error_message(response)}")
        elif action == "publish_advert":
            advert = get_object_or_404(OlxAdvert, id=request.POST.get("advert_id"), olx_user=olx_user)
            response = _olx_request(
                olx_user,
                "POST",
                f"/adverts/{advert.advert_id}/commands",
                json={"command": "activate"},
            )
            if response.status_code == 204:
                details = _olx_request(olx_user, "GET", f"/adverts/{advert.advert_id}")
                if details.status_code == 200:
                    advert = _upsert_advert(olx_user, _advert_data(details))
                advert.status = "active"
                advert.save(update_fields=["status"])
                messages.success(request, "OLX advert publication requested.")
            else:
                messages.error(request, f"OLX advert publication failed: {_olx_error_message(response)}")
            return redirect("olx-account-adverts", account_id=olx_user.id)
        elif action == "pushup_now":
            advert = get_object_or_404(OlxAdvert, id=request.POST.get("advert_id"), olx_user=olx_user)
            response = _olx_request(
                olx_user,
                "POST",
                f"/adverts/{advert.advert_id}/paid-features",
                json={
                    "code": "pushup",
                    "payment_method": advert.pushup_payment_method or "account",
                },
            )
            if response.status_code == 204:
                advert.last_pushup_at = timezone.now()
                advert.next_pushup_at = advert.calculate_next_pushup_at(advert.last_pushup_at)
                advert.last_pushup_error = ""
                advert.save(update_fields=["last_pushup_at", "next_pushup_at", "last_pushup_error"])
                messages.success(request, "OLX advert pushed up.")
            else:
                advert.last_pushup_error = _olx_error_message(response)[:2000]
                advert.save(update_fields=["last_pushup_error"])
                messages.error(request, f"OLX pushup failed: {_olx_error_message(response)}")
            return redirect("olx-account-adverts", account_id=olx_user.id)
        elif action == "delete_advert":
            advert = get_object_or_404(OlxAdvert, id=request.POST.get("advert_id"), olx_user=olx_user)
            response = _olx_request(olx_user, "DELETE", f"/adverts/{advert.advert_id}")
            if response.status_code != 204 and advert.status == "active":
                deactivate = _olx_request(
                    olx_user,
                    "POST",
                    f"/adverts/{advert.advert_id}/commands",
                    json={"command": "deactivate", "is_success": False},
                )
                if deactivate.status_code == 204:
                    response = _olx_request(olx_user, "DELETE", f"/adverts/{advert.advert_id}")
            if response.status_code == 204:
                advert.delete()
                messages.success(request, "OLX advert deleted.")
            else:
                messages.error(request, f"OLX advert delete failed: {_olx_error_message(response)}")
            return redirect("olx-account-adverts", account_id=olx_user.id)
        elif action == "update_pushup":
            advert = get_object_or_404(OlxAdvert, id=request.POST.get("advert_id"), olx_user=olx_user)
            days = int(request.POST.get("pushup_interval_days") or 0)
            advert.pushup_interval_days = days
            advert.pushup_enabled = days > 0
            advert.pushup_time = parse_time(request.POST.get("pushup_time") or "") or advert.pushup_time
            advert.next_pushup_at = advert.calculate_next_pushup_at()
            advert.last_pushup_error = ""
            advert.save(
                update_fields=[
                    "pushup_interval_days",
                    "pushup_enabled",
                    "pushup_time",
                    "next_pushup_at",
                    "last_pushup_error",
                ],
            )
            messages.success(request, "Pushup schedule updated.")
            return redirect("olx-account-adverts", account_id=olx_user.id)
        else:
            form = OlxAdvertForm(client_domain=client_domain)
    else:
        edit_id = request.GET.get("edit")
        copy_id = request.GET.get("copy")
        if edit_id:
            edit_advert = get_object_or_404(OlxAdvert, id=edit_id, olx_user=olx_user)
            form = OlxAdvertForm(initial=olx_advert_initial(edit_advert), client_domain=client_domain)
        elif copy_id:
            copy_advert = get_object_or_404(OlxAdvert, id=copy_id, olx_user=olx_user)
            details = _olx_request(olx_user, "GET", f"/adverts/{copy_advert.advert_id}")
            if details.status_code == 200:
                copy_advert = _upsert_advert(olx_user, _advert_data(details))
            form = OlxAdvertForm(initial=olx_advert_initial(copy_advert), client_domain=client_domain)
        else:
            form = OlxAdvertForm(client_domain=client_domain)

    adverts = olx_user.adverts.select_related("category", "city", "district").all()
    regions = OlxRegion.objects.filter(client_domain=client_domain)
    cities = OlxCity.objects.filter(client_domain=client_domain).select_related("region")
    districts = OlxDistrict.objects.filter(client_domain=client_domain).select_related("city")
    selected_region_id = str(form["region"].value() or "")
    selected_city_id = str(form["city"].value() or "")
    selected_district_id = str(form["district"].value() or "")
    return render(
        request,
        "olx/adverts.html",
        {
            "olx_user": olx_user,
            "adverts": adverts,
            "form": form,
            "edit_advert": edit_advert,
            "copy_advert": copy_advert,
            "regions": regions,
            "cities": cities,
            "districts": districts,
            "selected_region_id": selected_region_id,
            "selected_city_id": selected_city_id,
            "selected_district_id": selected_district_id,
        },
    )

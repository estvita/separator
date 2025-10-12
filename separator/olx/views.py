from django.db.models import Q
from django.contrib import messages
from django.shortcuts import render, get_object_or_404, redirect
from separator.decorators import login_message_required, user_message

import separator.bitrix.utils as bitrix_utils

from .models import OlxApp, OlxUser

@login_message_required(code="olx")
def olx_accounts(request):
    connector_service = "olx"
    portals, instances, lines = bitrix_utils.get_instances(request, connector_service)
    if not instances:
        user_message(request, "install_olx")

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
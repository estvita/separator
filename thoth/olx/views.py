from django.shortcuts import render, get_object_or_404
from django.contrib import messages

from thoth.bitrix.models import Line
import thoth.bitrix.utils as bitrix_utils

from .models import OlxApp, OlxUser
from thoth.decorators import login_message_required, user_message

@login_message_required(code="olx")
def olx_accounts(request):
    connector_service = "olx"
    olx_accounts = OlxUser.objects.filter(owner=request.user)
    olx_lines = Line.objects.filter(owner=request.user, connector__service=connector_service)
    instances = bitrix_utils.get_instances(request, connector_service)
    if not instances:
        user_message(request, "install_olx")

    olx_apps = OlxApp.objects.all()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "connect":
            olx_app_id = request.POST.get("olx_app")
            olx_app = OlxApp.objects.get(id=olx_app_id)
            return render(request, "olx/redirect_page.html", {"auth_link": olx_app.authorization_link})
        else:
            olx_id = request.POST.get("olx_id")
            line_id = request.POST.get("line_id")
            olx_user = get_object_or_404(OlxUser, id=olx_id, owner=request.user)
            try:
                bitrix_utils.connect_line(request, line_id, olx_user, connector_service)
            except Exception as e:
                messages.error(request, str(e))

    return render(request, "olx/accounts.html", 
        {
            "olx_accounts": olx_accounts,
            "olx_apps": olx_apps,
            "instances": instances,
            "olx_lines": olx_lines,
        }
    )
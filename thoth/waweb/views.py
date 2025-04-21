import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import WaSession, WaServer
from django.contrib import messages
from django.conf import settings
from .forms import SendMessageForm
from django.utils import timezone
from thoth.tariff.utils import get_trial
from thoth.bitrix.models import AppInstance, Line
import thoth.bitrix.crest as bitrix
from thoth.bitrix.utils import messageservice_add

from .tasks import send_message_task

WABWEB_SRV = settings.WABWEB_SRV

@login_required
def wa_sessions(request):
    sessions = WaSession.objects.filter(owner=request.user)
    app_instances = AppInstance.objects.filter(
        app__name="waweb",
        portal__owner=request.user,
    )

    # Проверка наличия активных сессий
    for session in sessions:
        if session.status == "open":
            session.show_link = True
        else:
            session.show_link = False

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "link":
            session_id = request.POST.get("session_id")
            app_instance_id = request.POST.get("app_instance")

            try:
                wa_session = WaSession.objects.get(id=session_id, owner=request.user)
                app_instance = AppInstance.objects.get(id=app_instance_id, owner=request.user)
                if wa_session.line:
                    # если подключено к существующему порталу
                    if wa_session.line.app_instance == app_instance:
                        messages.error(request, "Номер уже подключен к этмоу порталу.")
                        return redirect("waweb:wa_sessions")
                    # если подключено к другому порталу, то удалить линию
                    old_line = wa_session.line
                    wa_session.line = None
                    bitrix.call_method(old_line.app_instance, "imopenlines.config.delete",
                        {"CONFIG_ID": old_line.line_id})
                    
                line_data = {
                    "PARAMS": {
                        "LINE_NAME": wa_session.phone
                    }
                }
                create_line = bitrix.call_method(app_instance, "imopenlines.config.add", line_data)
                if "result" in create_line:
                    messages.success(request, create_line.get("result"))
                    line = Line.objects.create(
                        line_id=create_line["result"],
                        app_instance=app_instance,
                    )
                    wa_session.line = line
                    wa_session.app_instance = app_instance
                    wa_session.save()

                    payload = {
                        "CONNECTOR": "thoth_waweb",
                        "LINE": line.line_id,
                        "ACTIVE": 1,
                    }
                    activate_resp = bitrix.call_method(app_instance, "imconnector.activate", payload)
                    if activate_resp.get("error"):
                        messages.error(request, activate_resp.get("error"))
                        return redirect("waweb:wa_sessions")
                    else:
                        messages.success(request, activate_resp.get("result"))
                else:
                    messages.error(request, create_line)                            
                    return redirect("waweb:wa_sessions")
                    
            
                if wa_session.sms_service:
                    owner = request.user
                    if not hasattr(owner, 'auth_token'):
                        wa_session.sms_service = False
                        wa_session.save()
                        messages.error(request, f"API key not found for user {owner}. Operation aborted.")
                        return redirect("waweb:wa_sessions")
                    api_key = owner.auth_token.key
                    resp = messageservice_add(app_instance, wa_session.phone, wa_session.line.line_id, api_key, 'waweb')
                    if 'error' in resp:
                        wa_session.sms_service = False
                        wa_session.save()
                        messages.error(request, resp.get("error"))
                    else:
                        messages.success(request, resp.get("result"))
                return redirect("waweb:wa_sessions")
                    
            
            except AppInstance.DoesNotExist:
                messages.error(request, "AppInstance does not exist:", app_instance_id)
                print("AppInstance does not exist:", app_instance_id)
            except Exception as e:
                messages.error(request, "Unexpected error:", str(e))
                print("Unexpected error:", str(e))


    return render(request, 'waweb/wa_sessions.html', 
                  {'sessions': sessions,
                   "app_instances": app_instances,})


@login_required
def connect_number(request, session_id=None):
    if not session_id:
        # Создаем новую сессию
        new_session = WaSession.objects.create(owner=request.user)
        session_id = new_session.session

        # период
        if not new_session.date_end:
            new_session.date_end = get_trial(request.user, "waweb")
        new_session.save()

    wa_server = WaServer.objects.get(id=WABWEB_SRV)
    headers = {"apikey": wa_server.api_key}
    # Отправляем запрос на старт сессии
    payload = {
        "instanceName": str(session_id),
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "alwaysOnline": wa_server.always_online,
        "groupsIgnore": wa_server.groups_ignore,
        "readMessages": wa_server.read_messages,
    }
    response = requests.post(f"{wa_server.url}instance/create", json=payload, headers=headers)

    if response.status_code == 201:
        inst_data = response.json()
        instanceId = inst_data.get("instance", {}).get("instanceId")
        new_session.instanceId = instanceId
        new_session.save()
        img_data = inst_data.get("qrcode", {}).get("base64", "")
        if img_data:
            img_data = img_data.split(",", 1)[1]
            request.session['qr_image'] = img_data
            return redirect('waweb:qr_code_page', session_id=session_id)
        else:
            url = f"{wa_server.url}instance/delete/{session_id}"
            del_data = requests.delete(url, headers=headers)
            new_session.delete()
            messages.error(request, "Failed to initiate session.")
    else:
        url = f"{wa_server.url}instance/delete/{session_id}"
        del_data = requests.delete(url, headers=headers)
        new_session.delete()
        messages.error(request, "Failed to initiate session.")
    return redirect('waweb:wa_sessions')


@login_required
def qr_code_page(request, session_id):
    qr_image = request.session.pop('qr_image', '')
    if not qr_image:
        wa_server = WaServer.objects.get(id=WABWEB_SRV)
        gr_url = f"{wa_server.url}instance/connect/{session_id}"
        headers = {"apikey": wa_server.api_key}
        response = requests.get(gr_url, headers=headers)
        if response.status_code == 200:
            inst_data = response.json()
            img_data = inst_data.get("base64", "")
            if img_data:
                qr_image = img_data.split(",", 1)[1]
            else:
                messages.error(request, "Failed to restart session.")
                return redirect('waweb:wa_sessions')
        else:
            messages.error(request, "Failed to restart session.")
            return redirect('waweb:wa_sessions')
    return render(request, 'waweb/qr_code.html', {
        'session_id': session_id,
        'qr_image': qr_image,
    })



@login_required
def send_message_view(request, session_id):
    session = get_object_or_404(WaSession, session=session_id, owner=request.user)

    if timezone.now() > session.date_end:
        messages.error(request, f'Срок дествия вашего тарифа истек {session.date_end}')
        return redirect('waweb:wa_sessions')

    if request.method == "POST":
        if session.status == 'close':
            messages.error(request, "Телефон не подключен. Необходимо произвести повторное подключение.")
            return redirect('waweb:wa_sessions')
        form = SendMessageForm(request.POST)
        if form.is_valid():
            recipients_raw = form.cleaned_data['recipients']
            message = form.cleaned_data['message']
            recipients = [line.strip() for line in recipients_raw.splitlines() if line.strip()]
            
            send_message_task.delay(str(session.session), recipients, message, "string", True)
            
            messages.success(request, "Задача на отправку сообщений создана.")
            return redirect('waweb:wa_sessions')
    else:
        form = SendMessageForm()

    return render(request, 'waweb/send_message.html', {
        'form': form,
        'session': session,
    })
    
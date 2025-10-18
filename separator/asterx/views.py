from django import forms
from django.conf import settings
from django.contrib import messages
from django.utils.translation import gettext as _
from rest_framework.authtoken.models import Token
from django.contrib.auth.decorators import login_required
from django.forms import ModelForm, modelformset_factory
from django.shortcuts import render, redirect, get_object_or_404
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from separator.decorators import user_message
from separator.asterx.models import Server, Context, Settings
from separator.bitrix.models import User as BitrixUser, Credential, AppInstance, Bitrix


def get_portal_settings(member_id):
    portal = Bitrix.objects.filter(member_id=member_id).first()
    if not portal:
        return None
    app_instance = AppInstance.objects.filter(
        portal=portal, app__asterx=True
    ).first()
    if not app_instance:
        return None
    settings, _ = Settings.objects.get_or_create(app_instance=app_instance)
    return settings

SHOW_CARD_CHOICES = [
    (0, _('Not show')),
    (1, _('On call')),
    (2, _('On answer'))
]

class SettingsForm(forms.ModelForm):
    default_user_id = forms.IntegerField(
        required=False,
        label='Default Usuer ID'
    )
    show_card = forms.TypedChoiceField(
        choices=SHOW_CARD_CHOICES,
        widget=forms.RadioSelect,
        coerce=int,
        label='Show Card'
    )
    crm_create = forms.BooleanField(
        required=False,
        label='Create CRM'
    )
    vm_send = forms.BooleanField(
        required=False,
        label='Send VoiceMail'
    )
    smart_route = forms.BooleanField(
        required=False,
        label="Forwarding to manager"
    )

    class Meta:
        model = Settings
        fields = ['default_user_id', 'show_card', 'crm_create', 'vm_send', 'smart_route']

@login_required
def server_list(request):
    b24_data = request.session.pop('b24_data', None)
    member_id = None
    if b24_data:
        member_id = b24_data.get('member_id')
        if member_id:
            request.session['member_id'] = member_id

    portal_settings = None
    # POST "Обновить пользователей"
    if request.method == "POST" and "refresh_users" in request.POST:
        server_id = request.POST.get("server_id")
        server = Server.objects.filter(id=server_id, owner=request.user).first()
        if not server or not server.setup_complete:
            messages.error(request, "Сервер не найден или еще не подключен")
            return redirect('asterx')
        async_to_sync(get_channel_layer().group_send)(
            f"server_{server_id}",
            {"type": "send_event", "message": {"event": "refresh_users"}}
        )
        messages.success(request, "Обновление пользователей запущено.")
        return redirect('asterx')

    # POST "Добавить сервер"
    if request.method == "POST" and "add_server" in request.POST:
        exists_incomplete = Server.objects.filter(
            owner=request.user,
            setup_complete=False
        ).exists()
        if exists_incomplete:
            messages.error(request, "У вас уже есть неподключённый сервер!")
            return redirect('asterx')
        if member_id:
            portal_settings = get_portal_settings(member_id)
        # Всё ок - создаём новый сервер
        server = Server.objects.create(owner=request.user, settings=portal_settings)
        return redirect('asterx')

    settings_form = None

    member_id = request.session.get('member_id', None)
    if member_id:
        portal_settings = get_portal_settings(member_id)
        if request.method == "POST" and "save_settings" in request.POST:
            settings_form = SettingsForm(request.POST, instance=portal_settings)
            if settings_form.is_valid():
                settings_form.save()
                servers = Server.objects.filter(settings=portal_settings, owner=request.user).all()
                # --- Websocket эвент ---
                for server in servers:
                    channel_layer = get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"server_{server.id}",
                        {"type": "send_event", "message": {
                            "event": "settings_update",
                            "show_card": portal_settings.show_card,
                            "crm_create": portal_settings.crm_create,
                            "vm_send": portal_settings.vm_send,
                            "smart_route": portal_settings.smart_route,
                        }}
                    )
                messages.success(request, 'Settings saved and sent to client!')
                return redirect('asterx')
        else:
            settings_form = SettingsForm(instance=portal_settings)

    #     servers = Server.objects.filter(owner=request.user, settings=portal_settings)
    # else:
    servers = Server.objects.filter(owner=request.user)
    user_message(request, "asterx_info")

    return render(
        request,
        "asterx/list.html",
        {
            "servers": servers,
            "settings_form": settings_form,
            "portal_settings": portal_settings
        }
    )


class ServerEditForm(ModelForm):
    class Meta:
        model = Server
        fields = ['name', 'settings']

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user:
            self.fields['settings'].queryset = Settings.objects.filter(
                app_instance__app__asterx=True, app_instance__owner=user)

class ContextTypeForm(ModelForm):
    class Meta:
        model = Context
        fields = ['type']


@login_required
def edit_asterx(request, server_id):
    try:
        server = Server.objects.get(id=server_id, owner=request.user)
    except Exception:
        return redirect("asterx")
    contexts_qs = Context.objects.filter(server=server)
    ContextFormSet = modelformset_factory(Context, form=ContextTypeForm, extra=0, can_delete=True)
    if request.method == "POST":
        form = ServerEditForm(request.POST, instance=server, user=request.user)
        formset = ContextFormSet(request.POST, queryset=contexts_qs)

        old_settings_id = server.settings_id

        if form.is_valid() and formset.is_valid():
            srv = form.save()
            formset.save()
            channel_layer = get_channel_layer()
            payload = None
            if old_settings_id != srv.settings_id:
                if srv.settings:
                    settings = srv.settings
                    portal = settings.app_instance.portal
                    bitrix_user = BitrixUser.objects.filter(owner=server.owner, bitrix=portal).first()
                    access_token = None
                    if bitrix_user:
                        credential = Credential.objects.filter(
                            user=bitrix_user,
                            app_instance=settings.app_instance,
                        ).first()
                        if credential:
                            access_token = credential.access_token

                    user_token = None
                    if server.owner:
                        try:
                            user_token, _ = Token.objects.get_or_create(user=server.owner)
                            user_token = user_token.key
                        except Token.DoesNotExist:
                            user_token = None
                    payload = {
                        "event": "setup_complete",
                        "member_id": portal.member_id,
                        "protocol": portal.protocol,
                        "domain": portal.domain,
                        "access_token": access_token,
                        "user_token": user_token,
                        "show_card": settings.show_card,
                        "crm_create": settings.crm_create,
                        "vm_send": settings.vm_send,
                        "default_user_id": settings.default_user_id,
                    }
                else:
                    payload = {
                        "event": "app_disabled",
                    }

            if payload:
                async_to_sync(channel_layer.group_send)(
                    f"server_{server.id}",
                    {"type": "send_event", "message": payload}
                )
            contexts = Context.objects.filter(server=server)
            context_types = [{c.context: c.type} for c in contexts]
            async_to_sync(channel_layer.group_send)(
                f"server_{server.id}",
                {"type": "send_event", "message": {"event": "contexts_updated", "contexts": context_types}}
            )
            messages.success(request, "Server updated successfully!")
            return redirect('edit_asterx', server_id=server.id)
    else:
        form = ServerEditForm(instance=server, user=request.user)
        formset = ContextFormSet(queryset=contexts_qs)
    readonly_fields = {
        "id": server.id,
        "version": server.version,
        "system": server.system,
        "date_end": server.date_end,
    }
    return render(request, "asterx/edit.html", {
        "form": form,
        "formset": formset,
        "server": server,
        "readonly_fields": readonly_fields,
    })


@login_required
def app_settings(request, id):
    try:
        settings_instance = Settings.objects.get(id=id, app_instance__owner=request.user)
    except Exception:
        return redirect("asterx")
    
    if request.method == 'POST':
        form = SettingsForm(request.POST, instance=settings_instance)
        if form.is_valid():
            form.save()

            servers = Server.objects.filter(settings=settings_instance)
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()

            for server in servers:
                async_to_sync(channel_layer.group_send)(
                    f"server_{server.id}",
                    {"type": "send_event", "message": {
                        "event": "settings_update",
                        "show_card": settings_instance.show_card,
                        "crm_create": settings_instance.crm_create,
                        "vm_send": settings_instance.vm_send,
                        "smart_route": settings_instance.smart_route,
                        "default_user_id": settings_instance.default_user_id,
                    }}
                )

            messages.success(request, "Settings updated successfully.")
            return redirect('asterx')
    else:
        form = SettingsForm(instance=settings_instance)

    return render(request, "asterx/settings.html", {
        "form": form,
        "settings_instance": settings_instance
    })
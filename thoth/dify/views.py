import requests
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django import forms
from django.conf import settings
from django.contrib import messages
from .models import Dify
from thoth.tariff.utils import get_trial

import thoth.chatwoot.utils as chatwoot

class DifyForm(forms.ModelForm):
    class Meta:
        model = Dify
        fields = ['type', 'base_url', 'api_key']

@login_required
def dify_list_view(request):
    bots = Dify.objects.filter(owner=request.user)
    return render(request, 'dify/dify_list.html', {'bots': bots})

@login_required
def dify_form_view(request, dify_id=None):
    if dify_id:
        bot = get_object_or_404(Dify, id=dify_id, owner=request.user)
    else:
        bot = None

    if request.method == 'POST':
        form = DifyForm(request.POST, instance=bot)
        if form.is_valid():
            instance = form.save(commit=False)
            instance.owner = request.user
            url = form.cleaned_data['base_url'].rstrip('/') + '/info'
            api_key = form.cleaned_data['api_key']
            headers = {"Authorization": f"Bearer {api_key}"}
            try:
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    if not dify_id:
                        instance.expiration_date = get_trial(request.user, "dify")
                        instance.save()
                        messages.success(request, 'Бот успешно создан.')
                    else:
                        messages.success(request, 'Бот успешно обновлён.')

                    if settings.CHATWOOT_ENABLED and not instance.agent_bot and instance.type != "workflow":
                        agent_bot = chatwoot.create_bot(request.user, f"Dify bot: {instance.id}", instance.id, "dify")
                        if agent_bot:
                            instance.agent_bot = agent_bot
                            instance.save(update_fields=['agent_bot'])
                    return redirect('dify:dify_list')
                else:
                    msg = response.json().get('message') or response.text or 'Ошибка запроса к серверу Dify'
                    messages.error(request, f'Ошибка сервера: {msg}')
            except Exception as e:
                messages.error(request, f'Ошибка соединения с сервером: {e}')
    else:
        form = DifyForm(instance=bot)
    return render(request, 'dify/dify_form.html', {'form': form, 'bot': bot})

@login_required
def dify_delete(request, dify_id):
    bot = get_object_or_404(Dify, id=dify_id, owner=request.user)
    if request.method == 'POST':
        bot.delete()
        messages.success(request, 'Бот успешно удалён.')
        if bot.agent_bot:
            try:
                resp = chatwoot.delete_bot(request.user, bot.agent_bot.id)
                if resp.status_code == 200:
                    messages.success(request, f"Бот '{dify_id}' успешно удалён из чата.")
                else:
                    messages.warning(
                        request,
                        f"Не удалось удалить бота '{dify_id}' из чата. Код ответа: {resp.status_code}"
                    )
            except Exception as e:
                messages.warning(request, f"Ошибка при удалении бота из чата: {str(e)}")
    return redirect('dify:dify_list')
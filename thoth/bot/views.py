from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from openai import OpenAI
from .models import ApiKey, Bot, Voice, Feature
from .forms import ApiKeyForm, BotForm, VoiceForm, FeatureForm
from thoth.tariff.utils import get_trial
import thoth.chatwoot.utils as chatwoot
from thoth.bot.utils import get_tools_for_bot


def check_openai_api_key(api_key):
    client = OpenAI(api_key=api_key)
    try:
        client.models.list()
    except Exception as e:
        return e
    return True


@login_required
def bot_delete(request, bot_id):
    bot = get_object_or_404(Bot, id=bot_id, owner=request.user)

    try:
        if bot.agent_bot:
            resp = chatwoot.delete_bot(request.user, bot.agent_bot.id)
            if resp.status_code == 200:
                messages.success(request, f"Бот '{bot.name}' успешно удалён из чата.")
            else:
                messages.warning(
                    request,
                    f"Не удалось удалить бота '{bot.name}' из чата. Код ответа: {resp.status_code}"
                )
    except Exception as e:
        messages.warning(request, f"Ошибка при удалении бота из Chatwoot: {str(e)}")

    # Удаление ассистента из OpenAI с обработкой ошибок
    try:
        client = OpenAI(api_key=bot.token.key)
        client.beta.assistants.delete(bot.assistant_id)
        messages.success(request, f"Бот '{bot.name}' успешно удалён из OpenAI.")
    except Exception as e:
        messages.warning(request, f"Ошибка при удалении ассистента в OpenAI: {str(e)}")

    # Удаление локальных сущностей
    if bot.agent_bot is not None:
        bot.agent_bot.delete()
    bot.delete()
    messages.success(request, f"Бот '{bot.name}' успешно удалён из локальной базы.")

    return redirect('/bots')


@login_required
def bot_list_view(request):
    bots = Bot.objects.filter(owner=request.user)
    return render(request, 'bot_list.html', {'bots': bots})


@login_required
def bot_form_view(request, bot_id=None):
    bot = Bot.objects.get(id=bot_id, owner=request.user) if bot_id else None
    api_key = bot.token if bot else ApiKey.objects.filter(owner=request.user).first()

    if request.method == 'POST':
        api_key_form = ApiKeyForm(request.POST, instance=api_key)
        bot_form = BotForm(request.POST, instance=bot, user=request.user)

        try:
            new_api_key = api_key_form.save(commit=False)
            new_api_key.owner = request.user

            # Проверяем ключ
            key_check = check_openai_api_key(new_api_key.key)
            if key_check is not True:
                messages.error(request, f"Ошибка проверки API ключа: {key_check}")
                return redirect('/bots')
            
            if api_key and api_key.key != new_api_key.key:
                api_key.key = new_api_key.key
                api_key.save()
            else:
                api_key = new_api_key
                api_key.save()

            # Сохраняем или обновляем бота
            bot = bot_form.save(commit=False)
            bot.owner = request.user
            bot.token = api_key

            bot.save()

            selected_features = bot_form.cleaned_data['functions']
            bot.features.set(selected_features)

            tools = get_tools_for_bot(request.user, bot, "text") if bot else []
            client = OpenAI(api_key=api_key.key)

            if not bot.assistant_id:  # Если assistant_id отсутствует, создаём новый
                assistant = client.beta.assistants.create(
                    name=bot.name,
                    instructions=bot.system_message,
                    model=bot.model.name,
                    tools=tools,
                    temperature=float(bot.temperature),
                )
                bot.assistant_id = assistant.id
            else:  # Если assistant_id уже есть, обновляем существующий ассистент
                upd_assistant = client.beta.assistants.update(
                    bot.assistant_id,
                    name=bot.name,
                    instructions=bot.system_message,
                    model=bot.model.name,
                    tools=tools,
                    temperature=float(bot.temperature),
                )

            if not bot.vector_store:
                vector_store = client.vector_stores.create(name=bot.name)
                bot.vector_store = vector_store.id
                assistant = client.beta.assistants.update(
                    assistant_id=bot.assistant_id,
                    tool_resources={"file_search": {"vector_store_ids": [vector_store.id]}},
                )

            if settings.CHATWOOT_ENABLED and not bot.agent_bot:
                agent_bot = chatwoot.create_bot(request.user, bot.name, bot.id)
                if agent_bot:
                    bot.agent_bot = agent_bot
            
            if not bot.expiration_date:
                bot.expiration_date = get_trial(request.user, "bot")
            api_key.save()
            bot.save()

            messages.success(request, "Настройки ИИ бота сохранены.")
            return redirect('/bots')
        except Exception as e:
            messages.error(request, f"Произошла ошибка: {str(e)}")
            return redirect('/bots')

    else:
        api_key_form = ApiKeyForm(instance=api_key)
        bot_form = BotForm(instance=bot, user=request.user)

    return render(request, 'bot_form.html', {
        'api_key_form': api_key_form,
        'bot_form': bot_form,
    })


@login_required
def voice_list_view(request):
    voices = Voice.objects.filter(owner=request.user)
    return render(request, 'voice_list.html', {'voices': voices})


@login_required
def voice_form_view(request, voice_id=None):
    voice = Voice.objects.get(id=voice_id, owner=request.user) if voice_id else None
    api_key = voice.token if voice else ApiKey.objects.filter(owner=request.user).first()


    if request.method == 'POST':
        api_key_form = ApiKeyForm(request.POST, instance=api_key)
        voice_form = VoiceForm(request.POST, instance=voice, user=request.user)

        try:
            new_api_key = api_key_form.save(commit=False)
            new_api_key.owner = request.user

            # Проверяем ключ
            key_check = check_openai_api_key(new_api_key.key)
            if key_check is not True:
                messages.error(request, f"Ошибка проверки API ключа: {key_check}")
                return redirect('/voices')
            
            if api_key and api_key.key != new_api_key.key:
                api_key.key = new_api_key.key
                api_key.save()
            else:
                api_key = new_api_key
                api_key.save()

            # Сохраняем или обновляем бота
            voice = voice_form.save(commit=False)
            voice.owner = request.user
            voice.token = api_key

            if not voice.expiration_date:
                voice.expiration_date = get_trial(request.user, "voice")

            selected_features = voice_form.cleaned_data['functions']
            voice.features.set(selected_features)
            voice.save()

            messages.success(request, "Настройки Voice сохранены.")
            return redirect('/voices')
        except Exception as e:
            messages.error(request, f"Произошла ошибка: {str(e)}")
            return redirect('/voices')
        
    else:
        api_key_form = ApiKeyForm(instance=api_key)
        voice_form = VoiceForm(instance=voice, user=request.user)

        return render(
            request, "voice_form.html",
            {
                "api_key_form": api_key_form,
                "voice_form": voice_form,
            }
        )
    
        
@login_required
def voice_delete(request, voice_id):
    voice = get_object_or_404(Voice, id=voice_id, owner=request.user)

    voice.delete()
    messages.success(request, f"Бот '{voice.name}' успешно удалён из локальной базы.")

    return redirect('/voices')


@login_required
def feature_list_view(request):
    features = Feature.objects.filter(owner=request.user, type="function", engine="voice")
    return render(request, 'feature/feature_list.html', {'features': features})

@login_required
def feature_form_view(request, feature_id=None):
    if feature_id:
        feature = get_object_or_404(Feature, id=feature_id, owner=request.user)
    else:
        feature = None

    if request.method == 'POST':
        form = FeatureForm(request.POST, instance=feature, user=request.user)
        if form.is_valid():
            instance = form.save(commit=False)
            instance.owner = request.user
            instance.privacy = 'private'
            instance.engine = 'voice'
            instance.type = 'function'
            instance.save()
            messages.success(request, 'Функция успешно сохранена.')
            return redirect('voice:feature_list')
    else:
        form = FeatureForm(instance=feature, user=request.user)
    return render(request, 'feature/feature_form.html', {'form': form, 'feature': feature})

@login_required
def feature_delete_view(request, feature_id):
    feature = get_object_or_404(Feature, id=feature_id, owner=request.user)
    if request.method == "POST":
        feature.delete()
        messages.success(request, 'Функция успешно удалена.')
    return redirect('voice:feature_list')
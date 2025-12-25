from django.views import View
from django.views.generic import ListView
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.forms import formset_factory
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.translation import gettext as _
from .models import ChatBot, Connector, Command, CommandLang
from .forms import ChatBotForm, ConnectorForm, CommandCreateForm, CommandLangForm
from separator.bitrix.crest import call_method

CommandLangFormSet = formset_factory(CommandLangForm, extra=1, can_delete=True)

class BotListView(LoginRequiredMixin, ListView):
    model = ChatBot
    template_name = "bitbot/list.html"
    paginate_by = 20
    def get_queryset(self):
        return ChatBot.objects.filter(owner=self.request.user).select_related("connector", "app_instance")

class ConnectorListView(LoginRequiredMixin, ListView):
    model = Connector
    template_name = "bitbot/connectors_list.html"
    paginate_by = 20
    def get_queryset(self):
        return Connector.objects.filter(owner=self.request.user).select_related("provider")

class ConnectorEditView(LoginRequiredMixin, View):
    template_name = "bitbot/connector_edit.html"
    def _get_connector(self, pk):
        if not pk:
            return None
        return get_object_or_404(Connector.objects.filter(owner=self.request.user), pk=pk)
    def get(self, request, pk=None):
        connector = self._get_connector(pk)
        form = ConnectorForm(instance=connector)
        return render(request, self.template_name, {"object": connector, "form": form})
    def post(self, request, pk=None):
        connector = self._get_connector(pk)
        if "save_connector" in request.POST:
            form = ConnectorForm(request.POST, instance=connector)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.owner = request.user
                obj.save()
                messages.success(request, _("Connector saved."))
                return redirect("connector_list")
            messages.error(request, _("Please fix form errors."))
            return render(request, self.template_name, {"object": connector, "form": form})
        if "delete_connector" in request.POST and connector:
            connector.delete()
            messages.success(request, _("Connector deleted."))
            return redirect("connector_list")
        return redirect("connector_list")

class BotEditView(LoginRequiredMixin, View):
    template_name = "bitbot/edit.html"
    def _get_bot(self, pk):
        if not pk:
            return None
        return get_object_or_404(ChatBot.objects.filter(owner=self.request.user).select_related("connector", "app_instance"), pk=pk)
    def get(self, request, pk=None):
        bot = self._get_bot(pk)
        form = ChatBotForm(instance=bot, user=request.user)
        ctx = {
            "object": bot,
            "form": form,
            "commands": bot.commands.all().prefetch_related("langs") if bot else [],
            "cmd_form": CommandCreateForm(),
            "lang_formset": CommandLangFormSet(prefix="lang"),
        }
        return render(request, self.template_name, ctx)

    @transaction.atomic
    def post(self, request, pk=None):
        bot = self._get_bot(pk)

        # Сохранение/обновление бота
        if "save_bot" in request.POST:
            form = ChatBotForm(request.POST, instance=bot, user=request.user)
            if form.is_valid():
                bot = form.save(commit=False)
                bot.owner = request.user
                bot.save()
                if bot.bot_id == 0:
                    domain = bot.app_instance.app.site.domain
                    payload = {
                        "CODE": f"bitbot_{bot.id}",
                        "TYPE": bot.bot_type,
                        "EVENT_HANDLER": f"https://{domain}/api/bitrix/",
                        "PROPERTIES": {
                            "NAME": bot.name,
                        },
                    }
                    try:
                        resp = call_method(bot.app_instance, "imbot.register", payload)
                        bot.bot_id = resp.get("result")
                        bot.save()
                    except Exception as e:
                        messages.error(request, e)
                
                if not bot.date_end and "separator.tariff" in settings.INSTALLED_APPS:
                    from separator.tariff.utils import get_trial
                    bot.date_end = get_trial(bot.owner, "bitbot")
                    bot.save()

                messages.success(request, _("Bot saved."))
                return redirect(reverse("bitbot_edit", kwargs={"pk": bot.pk}))
            messages.error(request, _("Please fix bot form errors."))
            return self.get(request, pk=bot.pk if bot else None)

        # Добавление команды
        if "add_command" in request.POST:
            if not bot:
                messages.error(request, _("Save the bot first."))
                return redirect("bitbot_add")
            cmd_form = CommandCreateForm(request.POST)
            lang_formset = CommandLangFormSet(request.POST, prefix="lang")
            if cmd_form.is_valid() and lang_formset.is_valid():
                valid_translations = []
                for f in lang_formset:
                    cd = f.cleaned_data or {}
                    if cd.get('DELETE'):
                        continue
                    lang = (cd.get("language") or "").strip()
                    title = (cd.get("title") or "").strip()
                    if lang and title:
                        valid_translations.append(cd)
                if not valid_translations:
                    messages.error(request, _("Add at least one translation (language and title)."))
                    return self.get(request, pk=bot.pk)

                cmd = cmd_form.save(commit=False)
                cmd.bot = bot
                cmd.save()

                CommandLang.objects.bulk_create([
                    CommandLang(
                        command=cmd,
                        language=cd["language"].strip(),
                        title=cd["title"].strip(),
                        params=(cd.get("params") or "").strip() or None,
                    ) for cd in valid_translations
                ])

                # Регистрация команды в Bitrix:
                if cmd.command_id == 0:
                    domain = bot.app_instance.app.site.domain
                    payload = {
                        "BOT_ID": bot.bot_id,
                        "COMMAND": cmd.command,
                        "COMMON": cmd.common, "HIDDEN": cmd.hidden, "EXTRANET_SUPPORT": cmd.extranet,
                        "EVENT_COMMAND_ADD": f"https://{domain}/api/bitrix/",
                        "LANG": [
                            {"LANGUAGE_ID": cd["language"].strip(), "TITLE": cd["title"].strip(), "PARAMS": (cd.get("params") or "").strip()}
                            for cd in valid_translations
                        ],
                    }
                    try:
                        resp = call_method(bot.app_instance, "imbot.command.register", payload)
                        cmd.command_id = int(resp["result"]); cmd.save(update_fields=["command_id"])
                        messages.success(request, _("Command added."))
                        return redirect(reverse("bitbot_edit", kwargs={"pk": bot.pk}))
                    except Exception as e:
                        messages.error(request, e)
            messages.error(request, _("Please fix command form/translations."))
            return self.get(request, pk=bot.pk)

        # Удаление команды
        if "delete_command" in request.POST and bot:
            cmd_id = request.POST.get("command_id")
            cmd = Command.objects.filter(id=cmd_id, bot=bot).first()
            if not cmd:
                messages.error(request, _("Command not found."))
                return redirect(reverse("bitbot_edit", kwargs={"pk": bot.pk}))

            # Удаление команды на портале Bitrix:
            if cmd.command_id:
                try:
                    resp = call_method(bot.app_instance, "imbot.command.unregister", {"COMMAND_ID": cmd.command_id})
                    cmd.delete()
                    messages.success(request, resp)
                except Exception as e:
                    messages.error(request, e)
            return redirect(reverse("bitbot_edit", kwargs={"pk": bot.pk}))

        # Удаление бота (опционально, если добавите кнопку)
        if "delete_bot" in request.POST and bot:
            if bot.bot_id:
                try:
                    resp = call_method(bot.app_instance, "imbot.unregister", {"BOT_ID": bot.bot_id})
                    bot.delete()
                    messages.success(request, resp)
                except Exception as e:
                    messages.error(request, e)
            return redirect("bitbot_list")

        return redirect("bitbot_list")
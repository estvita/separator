from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.utils import timezone
from thoth.bot.models import Voice, Bot
from thoth.bot.utils import get_tools_for_bot
from thoth.bot.tasks import message_processing

class BotHandler(GenericViewSet):
    def create(self, request, *args, **kwargs):
        data = request.data
        bot_id = request.query_params.get('id')
        if not bot_id:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)
        try:
            bot = Bot.objects.get(id=bot_id)
        except Bot.DoesNotExist:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)
        
        if bot.expiration_date and timezone.now() > bot.expiration_date:
            return Response("tariff has expired", status=status.HTTP_402_PAYMENT_REQUIRED)

        event = data.get('event')
        sender = data.get('sender', {})
        sender_type = sender.get('type')

        if event == "message_created" and sender_type != "agent_bot":
            message_processing.delay(data, bot_id)

        return Response({'message': 'message received'})
    

class VoiceDetails(GenericViewSet):
    def create(self, request, *args, **kwargs):
        bot_id = request.data.get("bot")
        if not bot_id:
            return Response({"error": "Bot ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        bot = get_object_or_404(Voice, id=bot_id)
        if timezone.now() > bot.expiration_date:
            return Response("tariff has expired", status=status.HTTP_402_PAYMENT_REQUIRED)
        
        tools = get_tools_for_bot(request.user, bot, "voice")
        
        response = {
            "flavor": bot.model.provider.name,
            bot.model.provider.name: {
                "model": bot.model.name,
                "key": bot.token.key if bot.token else None,
                "voice": bot.vocal.vocal,
                "instructions": bot.instruction,
                "welcome_message": bot.welcome_msg,
                "tools": tools,
                "transfer_uri": bot.transfer_uri,
                "temperature": bot.temperature,
                "max_tokens": bot.max_tokens
            }
        }
        
        return Response(response, status=status.HTTP_200_OK)
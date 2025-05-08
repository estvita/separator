from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone

import os
import requests
import threading
import redis
from urllib.parse import urlparse
from io import BytesIO

from openai import OpenAI

from thoth.bot.models import Voice, Bot
from thoth.bot.utils import get_tools_for_bot
from thoth.bot.tasks import message_processing

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)


class BotHandler(GenericViewSet):
    def create(self, request, *args, **kwargs):
        data = request.data
        bot_id = request.query_params.get('id')
        if not bot_id:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)

        try:
            bot = Bot.objects.get(id=bot_id, owner=request.user)
        except Bot.DoesNotExist:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)

        if bot.expiration_date and timezone.now() > bot.expiration_date:
            return Response("tariff has expired", status=status.HTTP_402_PAYMENT_REQUIRED)

        event = data.get('event')
        sender = data.get('sender', {})
        sender_type = sender.get('type')

        if event == "message_created" and sender_type != "agent_bot":
            message_type = data.get('message_type')
            content = data.get('content')
            client = OpenAI(api_key=bot.token.key)
            attachments = data.get('attachments', [])
            if attachments:
                for attachment in attachments:
                    if attachment.get('file_type') == "audio" and bot.speech_to_text:
                        data_url = attachment.get('data_url')
                        parsed_url = urlparse(data_url)
                        filename = os.path.basename(parsed_url.path)
                        response = requests.get(data_url, stream=True)
                        if response.status_code == 200:
                            audio_file = BytesIO(response.content)
                            audio_file.name = filename
                            content = client.audio.transcriptions.create(
                                model=bot.stt_model,
                                file=audio_file,
                                response_format="text",
                            )

            account = data.get('account', {})
            account_id = account.get('id')

            conversation = data.get('conversation', {})
            conversation_id = conversation.get('id')

            if not bot.agent_bot or not content:
                return Response("message should not be processed")

            meta = conversation.get('meta', {})
            sender_meta = meta.get('sender', {})
            labels = conversation.get('labels', [])
            contact_inbox = conversation.get('contact_inbox', {})
            contact_id = contact_inbox.get('contact_id')

            conversation_status = conversation.get('status')
            role = "user" if message_type == "incoming" else "assistant"
            redis_key = f"bot:{account_id}:{contact_id}"
            thread_id = redis_client.get(redis_key)

            self.debounce_handle(redis_key, content, thread_id, bot_id, role, conversation_status,
                                 message_type, labels, account_id, conversation_id, sender_meta.get('id'))
        return Response("Processed", status=status.HTTP_200_OK)

    def debounce_handle(self, redis_key, content, thread_id, bot_id, role,
                        conversation_status, message_type, labels, account_id, conversation_id, sender_id):
        debounce_time = 10
        buffer_key = f"buffer:{redis_key}"
        timer_key = f"timer:{redis_key}"

        redis_client.rpush(buffer_key, content)
        ttl = redis_client.ttl(timer_key)
        redis_client.setex(timer_key, debounce_time, '1')

        if ttl <= 0:
            def flush_messages():
                messages = redis_client.lrange(buffer_key, 0, -1)
                grouped_messages = '\n'.join(messages)
                redis_client.delete(buffer_key)
                redis_client.delete(timer_key)                
                message_processing.delay(
                    thread_id, redis_key, bot_id, role, grouped_messages, conversation_status,
                    message_type, labels, account_id, conversation_id, sender_id)

            threading.Timer(debounce_time, flush_messages).start()
    

class VoiceDetails(GenericViewSet):
    def create(self, request, *args, **kwargs):
        bot_id = request.data.get("bot")
        if not bot_id:
            return Response({"error": "Bot ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bot = Voice.objects.get(id=bot_id)
        except Voice.DoesNotExist:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)
        
        if timezone.now() > bot.expiration_date:
            return Response("tariff has expired", status=status.HTTP_402_PAYMENT_REQUIRED)
        
        tools = get_tools_for_bot(bot.owner, bot, "voice")
        
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
                "max_tokens": bot.max_tokens,
                "dify_url": bot.dify_workflow.base_url,
                "dify_key": bot.dify_workflow.api_key,
            }
        }
        
        return Response(response, status=status.HTTP_200_OK)
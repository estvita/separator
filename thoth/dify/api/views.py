import requests
import redis
from rest_framework.viewsets import ViewSet
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone

from thoth.dify.models import Dify

from thoth.dify.client import WorkflowClient, ChatClient

import thoth.chatwoot.utils as chatwoot


redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)


class DifyReceiver(ViewSet):
    def create(self, request):
        data = request.data
        bot_id = request.query_params.get('id')
        if not bot_id:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)
        try:
            bot = Dify.objects.get(id=bot_id, owner=request.user)
        except Dify.DoesNotExist:
            return Response("Bot not found", status=status.HTTP_404_NOT_FOUND)

        if bot.expiration_date and timezone.now() > bot.expiration_date:
            return Response("tariff has expired", status=status.HTTP_402_PAYMENT_REQUIRED)
        
        event = data.get('event')
        sender = data.get('sender', {})
        sender_type = sender.get('type')
        message_type = data.get('message_type')
        conversation = data.get('conversation', {})
        conversation_status = conversation.get('status')
        if event == "message_created" and sender_type != "agent_bot" and message_type == "incoming" and conversation_status != "open":
            content = data.get('content')
            account = data.get('account', {})
            account_id = account.get('id')
            conversation_id = conversation.get('id')
            contact_inbox = conversation.get('contact_inbox', {})
            contact_id = contact_inbox.get('contact_id')

            dify_response = None

            if bot.type == "workflow":
                inputs = {"content": content}
                workflow_client = WorkflowClient(bot.api_key, bot.base_url)

                try:
                    response = workflow_client.run(inputs, response_mode="blocking", user=contact_id)
                    response.raise_for_status()
                    response = response.json()
                    dify_response = response.get("data", {}).get("outputs", {}).get("text", "")
                except requests.RequestException as e:
                    print(f"Error sending response to chat: {e}")
                    return Response("Error sending the message.", status=response.status_code)

            elif bot.type == "chatflow":
                chatflow_client = ChatClient(bot.api_key, bot.base_url)
                redis_key = f"dify:{account_id}:{contact_id}"
                thread_id = redis_client.get(redis_key)
                try:
                    response = chatflow_client.create_chat_message(inputs={}, query=content, user=contact_id, conversation_id=thread_id)
                    response.raise_for_status()
                    response = response.json()
                    dify_response = response.get("answer")
                    if thread_id is None:
                        thread_id = response.get("conversation_id")
                        redis_client.set(redis_key, thread_id)
                except requests.RequestException as e:
                    return Response("Error sending the message.", status=response.status_code)

            if dify_response:
                payload = {
                    "content": dify_response,
                    "message_type": "outgoing"
                }
                msg_url = f"api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
                try:
                    resp = chatwoot.call_api(msg_url, data=payload, access_token=bot.agent_bot.token)
                    resp.raise_for_status()
                except requests.RequestException as e:
                    print(f"Error sending response to chat: {e}")
                    return Response("Error sending the message.", status=500)

            
        return Response({'received': data}, status=status.HTTP_200_OK)
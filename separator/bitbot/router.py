import redis
from celery import shared_task
from separator.bitrix.crest import call_method
from dify_client import ChatClient, WorkflowClient

from .models import ChatBot

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


def extract_values(data, keys):
    result = {}
    for search_key in keys:
        for key in data:
            if key.endswith(f'[{search_key}]'):
                result[search_key] = data[key]
                break
    return result


@shared_task(queue='bitbot')
def event_processor(data):
    try:
        member_id = data.get("auth[member_id]")
        application_token = data.get("auth[application_token]")
        inputs = extract_values(data, 
                                ['access_token', 'scope', 'client_endpoint', 'BOT_ID', 'DIALOG_ID', 
                                 'AUTHOR_ID', 'MESSAGE', 'COMMAND', 'COMMAND_PARAMS', 'CHAT_TITLE', 'LANGUAGE',
                                 'CHAT_ENTITY_DATA_1', 'CHAT_ENTITY_DATA_2', 'event', 'FIRST_NAME', 'LAST_NAME'])        
        bot_id = inputs.get("BOT_ID")
        dialog_id = inputs.get("DIALOG_ID")
        user_id = inputs.get("AUTHOR_ID")

        if not bot_id or not application_token:
            raise ValueError("Missing ID or token")

        chatbot = ChatBot.objects.filter(
            bot_id=bot_id,
            app_instance__application_token=application_token
        ).first()
        if not chatbot:
            raise LookupError(f"Bot {bot_id} not found")

        connector = chatbot.connector
        provider = connector.provider
        base_url = connector.url or provider.url

        answer_payload = {}

        if provider.type == "dify_chatflow":
            query = inputs.get("MESSAGE")
            chat_client = ChatClient(connector.key, base_url)

            redis_key = f"bitbot:{member_id}:{dialog_id}"
            conversation_id = redis_client.get(redis_key)
            if conversation_id:
                chat_response = chat_client.create_chat_message(
                    inputs=inputs, query=query, user=user_id, conversation_id=conversation_id.decode()
                )
            else:
                chat_response = chat_client.create_chat_message(
                    inputs=inputs, query=query, user=user_id
                )
            chat_response.raise_for_status()
            result = chat_response.json()

            if not conversation_id:
                conv_id = result.get("conversation_id")
                if conv_id:
                    redis_client.set(redis_key, conv_id)

            answer = result.get("answer")
            answer_payload = {
                "BOT_ID": bot_id,
                "DIALOG_ID": dialog_id,
                "MESSAGE": answer
            }
        elif provider.type == "dify_workflow":
            workflow_client = WorkflowClient(connector.key, base_url)
            return workflow_client.run(inputs=inputs, user=user_id)

        if answer_payload:
            return call_method(chatbot.app_instance, "imbot.message.add", answer_payload)
        else:
            raise Exception("Bot no answer")
    except Exception:
        raise
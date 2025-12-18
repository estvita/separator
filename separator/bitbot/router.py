import redis
import requests
from celery import shared_task
from django.utils import timezone
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

def send_command_answer(chatbot, command_id, message_id, text):
    if chatbot and command_id and message_id:
        return call_method(chatbot.app_instance, "imbot.command.answer", {
            "COMMAND_ID": command_id,
            "MESSAGE_ID": message_id,
            "MESSAGE": text,
        })

def send_message(chatbot, bot_id, dialog_id, text):
    if chatbot and bot_id and dialog_id:
        return call_method(chatbot.app_instance, "imbot.message.add", {
            "BOT_ID": bot_id,
            "DIALOG_ID": dialog_id,
            "MESSAGE": text,
        })

@shared_task(queue='bitbot')
def event_processor(data):
    command_id = None
    message_id = None
    bot_id = None
    dialog_id = None
    chatbot = None
    try:
        member_id = data.get("auth[member_id]")
        application_token = data.get("auth[application_token]")
        inputs = extract_values(data, [
            'access_token', 'scope', 'client_endpoint', 'BOT_ID', 'DIALOG_ID', 'AUTHOR_ID',
            'MESSAGE_ID', 'MESSAGE', 'COMMAND_ID', 'COMMAND', 'COMMAND_PARAMS', 'CHAT_TITLE',
            'LANGUAGE', 'CHAT_ENTITY_DATA_1', 'CHAT_ENTITY_DATA_2', 'CHAT_ENTITY_ID',
            'FIRST_NAME', 'LAST_NAME'
        ])

        bot_id = inputs.get("BOT_ID")
        dialog_id = inputs.get("DIALOG_ID")
        user_id = inputs.get("AUTHOR_ID")
        command_id = inputs.get("COMMAND_ID")
        command = inputs.get("COMMAND")
        message_id = inputs.get("MESSAGE_ID")
        query = inputs.get("MESSAGE")

        if not bot_id or not application_token:
            raise ValueError("Missing ID or token")

        chatbot = ChatBot.objects.filter(
            bot_id=bot_id,
            app_instance__application_token=application_token
        ).first()
        if not chatbot:
            raise LookupError(f"Bot {bot_id} not found")
        if chatbot.date_end and timezone.now() > chatbot.date_end:
            raise Exception({"license has expired"})

        connector = chatbot.connector
        provider = connector.provider
        base_url = connector.url or provider.url

        redis_key = f"bitbot:{member_id}:{bot_id}:{dialog_id}"
        session_id = redis_client.get(redis_key)

        def save_and_return_answer(answer, new_session_id=None):
            if new_session_id:
                redis_client.set(redis_key, new_session_id, ex=86400)
            if not answer:
                raise Exception("Bot no answer")
            if command:
                return send_command_answer(chatbot, command_id, message_id, answer)
            return send_message(chatbot, bot_id, dialog_id, answer)

        # DIFy Chatflow
        if provider.type == "dify_chatflow":
            chat_client = ChatClient(connector.key, base_url)
            kwargs = dict(inputs=inputs, query=query, user=user_id)
            if session_id:
                kwargs["conversation_id"] = session_id.decode()
            chat_response = chat_client.create_chat_message(**kwargs)
            chat_response.raise_for_status()
            result = chat_response.json()
            answer = result.get("answer")
            conversation_id = result.get("conversation_id")
            return save_and_return_answer(
                answer,
                conversation_id if not session_id else None,
            )

        # DIFy Workflow
        if provider.type == "dify_workflow":
            workflow_client = WorkflowClient(connector.key, base_url)
            response = workflow_client.run(
                inputs=inputs, user=user_id, response_mode="blocking"
            )
            response.raise_for_status()
            try:
                response_json = response.json()
            except ValueError:
                return save_and_return_answer(response.text)

            outputs = response_json.get("data", {}).get("outputs", {})
            answer = "No outputs" if not outputs else "\n".join(
                f"{k}: {v}" for k, v in outputs.items()
            )
            return save_and_return_answer(answer)

        # Typebot
        if provider.type == "typebot":
            message_type = "command" if command else "text"
            if command:
                query = command

            headers = {
                "Authorization": f"Bearer {connector.key}",
                "Content-Type": "application/json",
            }

            if session_id:
                url = f"{provider.url}/api/v1/sessions/{session_id.decode()}/continueChat"
                payload = {
                    "message": {
                        "type": message_type,
                        message_type: query,
                    },
                    "textBubbleContentFormat": "richText",
                }
            else:
                url = base_url
                payload = {
                    "message": {
                        "type": message_type,
                        message_type: query,
                    },
                    "isStreamEnabled": False,
                    "isOnlyRegistering": False,
                    "prefilledVariables": inputs,
                    "textBubbleContentFormat": "richText",
                }

            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            new_session_id = result.get("sessionId")

            messages = result.get("messages", [])
            answer = "The bot is not responding"
            if messages:
                rich = messages[-1].get("content", {}).get("richText", [])
                answer = " ".join(
                    child.get("text", "")
                    for el in rich for child in el.get("children", [])
                ) or answer

            return save_and_return_answer(
                answer,
                new_session_id if not session_id else None,
            )

        raise Exception("Unknown provider type")

    except Exception as e:
        error_msg = f"[B]Error[/B]: {str(e)}"
        send_command_answer(chatbot, command_id, message_id, error_msg)
        send_message(chatbot, bot_id, dialog_id, error_msg)
        raise
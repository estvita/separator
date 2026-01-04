import redis
import requests
from celery import shared_task
from django.utils import timezone
from separator.bitrix.crest import call_method
from dify_client import ChatClient, WorkflowClient
from django.conf import settings

from .models import ChatBot

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def extract_values(data, keys):
    result = {}
    for search_key in keys:
        for key in data:
            if key.endswith(f'[{search_key}]'):
                result[search_key] = data[key]
                break
    return result

def extract_files(data):
    files = {}
    for key, value in data.items():
        if key.startswith('data[PARAMS][FILES]['):
            try:
                # key format: data[PARAMS][FILES][<id>][<prop>]...
                parts = key.split('][')
                if len(parts) >= 4:
                    file_id = parts[2]
                    prop = parts[3].replace(']', '')
                    
                    if file_id not in files:
                        files[file_id] = {'id': file_id}
                    
                    if prop in ['name', 'type', 'size', 'extension']:
                        files[file_id][prop] = value
                    elif key.endswith('[viewerType]'):
                        # Extract viewerType from viewerAttrs
                        files[file_id]['viewerType'] = value
            except Exception:
                continue
    return list(files.values())

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
        
        # First extract BOT_ID to get bot-specific auth
        bot_id = data.get("data[PARAMS][BOT_ID]")
        if not bot_id:
            # Try alternative extraction method
            for key in data:
                if key.endswith('[BOT_ID]'):
                    bot_id = data[key]
                    break
        
        # Extract bot-specific auth credentials
        bot_access_token = None
        if bot_id:
            bot_access_token = data.get(f"data[BOT][{bot_id}][AUTH][access_token]")
        
        inputs = extract_values(data, [
            'scope', 'client_endpoint', 'DIALOG_ID', 'AUTHOR_ID',
            'MESSAGE_ID', 'MESSAGE', 'COMMAND_ID', 'COMMAND', 'COMMAND_PARAMS', 'CHAT_TITLE',
            'LANGUAGE', 'CHAT_ENTITY_DATA_1', 'CHAT_ENTITY_DATA_2', 'CHAT_ENTITY_ID',
            'FIRST_NAME', 'LAST_NAME', 'BOT_ID', 'CHAT_ID',
        ])
        
        files = extract_files(data)
        if files:
            # Pass the first file ID and its type as variables
            inputs['file_id'] = files[0]['id']
            if 'viewerType' in files[0]:
                inputs['file_type'] = files[0]['viewerType']

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

        # Get admin token from credentials
        credential = chatbot.app_instance.credentials.filter(user__admin=True).first()
        if not credential:
            credential = chatbot.app_instance.credentials.first()
        
        if credential:
            inputs['user_access_token'] = credential.access_token

        if bot_access_token:
            inputs['bot_access_token'] = bot_access_token

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
            # If message is empty but file is present, send file name as message
            if not query and inputs.get('file_id'):
                files = extract_files(data)
                if files:
                    file_name = files[0].get('name', 'File')
                    query = f"File sent: {file_name}"
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
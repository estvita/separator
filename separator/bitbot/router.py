import json

import redis
import requests
from celery import shared_task
from django.utils import timezone
from separator.bitrix.crest import call_method
from dify_client import ChatClient, WorkflowClient
from dify_client.exceptions import DifyClientError, ValidationError
from django.conf import settings

from .models import ChatBot

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def extract_values(data, keys):
    result = {}
    for search_key in keys:
        if search_key in data:
            result[search_key] = data[search_key]
            continue
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

def send_message(chatbot, bot_id, dialog_id, text, system="N"):
    if chatbot and bot_id and dialog_id:
        return call_method(chatbot.app_instance, "imbot.message.add", {
            "BOT_ID": bot_id,
            "DIALOG_ID": dialog_id,
            "MESSAGE": text,
            "SYSTEM": system
        })


def get_bot_id(data):
    bot_id = data.get("data[PARAMS][BOT_ID]")
    if bot_id:
        return bot_id
    for key, value in data.items():
        if key.endswith("[BOT_ID]"):
            return value
    return None


def get_application_token(data):
    return data.get("auth[application_token]")


def get_chatbot(data, bot_id=None, application_token=None):
    bot_id = bot_id or get_bot_id(data)
    application_token = application_token or get_application_token(data)
    if not bot_id or not application_token:
        return None
    return ChatBot.objects.filter(
        bot_id=bot_id,
        app_instance__application_token=application_token,
    ).first()


def get_batch_delay(data):
    chatbot = get_chatbot(data)
    if not chatbot:
        return 0
    return max(chatbot.batch_delay or 0, 0)


def set_event_value(data, key_name, value):
    for key in list(data.keys()):
        if key == key_name or key.endswith(f"[{key_name}]"):
            data[key] = value
            return
    data[key_name] = value


def get_buffer_prefix(data):
    member_id = data.get("auth[member_id]")
    dialog_id = extract_values(data, ["DIALOG_ID"]).get("DIALOG_ID")
    bot_id = get_bot_id(data)
    if not member_id or not bot_id or not dialog_id:
        return None
    return f"bitbot:buffer:{member_id}:{bot_id}:{dialog_id}"


def should_buffer_event(data, batch_delay=0):
    inputs = extract_values(data, ["event", "COMMAND_ID", "MESSAGE"])
    if inputs.get("event") != "ONIMBOTMESSAGEADD":
        return False
    if inputs.get("COMMAND_ID"):
        return False
    if extract_files(data):
        return False
    if not inputs.get("MESSAGE"):
        return False
    if batch_delay <= 0:
        return False
    return bool(get_buffer_prefix(data))


def merge_buffered_events(events):
    if not events:
        return None

    base_event = dict(events[-1])
    message_batch = []
    messages = []

    for item in events:
        values = extract_values(item, ["MESSAGE", "MESSAGE_ID", "AUTHOR_ID", "USER_ID"])
        text = values.get("MESSAGE")
        if text:
            messages.append(text)
        message_batch.append({
            "message": text or "",
            "message_id": values.get("MESSAGE_ID"),
            "author_id": values.get("AUTHOR_ID") or values.get("USER_ID"),
        })

    combined_message = "\n".join(messages).strip()
    set_event_value(base_event, "MESSAGE", combined_message)
    set_event_value(base_event, "MESSAGE_ID", message_batch[-1].get("message_id"))
    base_event["MESSAGE_BATCH"] = json.dumps(message_batch, ensure_ascii=False)
    base_event["MESSAGE_BATCH_SIZE"] = str(len(events))
    return base_event


def enqueue_buffered_event(data, batch_delay):
    buffer_prefix = get_buffer_prefix(data)
    if not buffer_prefix:
        return process_event_payload(data)

    messages_key = f"{buffer_prefix}:messages"
    version_key = f"{buffer_prefix}:version"
    buffer_ttl = max(batch_delay * 6, 60)
    version = redis_client.incr(version_key)

    pipe = redis_client.pipeline()
    pipe.rpush(messages_key, json.dumps(data, ensure_ascii=False))
    pipe.expire(messages_key, buffer_ttl)
    pipe.expire(version_key, buffer_ttl)
    pipe.execute()

    flush_buffered_events.apply_async(
        args=[buffer_prefix, version],
        countdown=batch_delay,
        queue="bitbot",
    )
    return {"status": "buffered", "version": int(version)}


def pop_buffered_events(buffer_prefix, expected_version):
    messages_key = f"{buffer_prefix}:messages"
    version_key = f"{buffer_prefix}:version"

    with redis_client.pipeline() as pipe:
        while True:
            try:
                pipe.watch(version_key, messages_key)
                current_version = pipe.get(version_key)
                if not current_version or int(current_version) != int(expected_version):
                    pipe.unwatch()
                    return []
                raw_events = pipe.lrange(messages_key, 0, -1)
                pipe.multi()
                pipe.delete(messages_key)
                pipe.delete(version_key)
                pipe.execute()
                return raw_events
            except redis.WatchError:
                continue


@shared_task(queue="bitbot")
def flush_buffered_events(buffer_prefix, version):
    raw_events = pop_buffered_events(buffer_prefix, version)
    if not raw_events:
        return None

    events = []
    for raw_event in raw_events:
        if isinstance(raw_event, bytes):
            raw_event = raw_event.decode("utf-8")
        events.append(json.loads(raw_event))

    combined_event = merge_buffered_events(events)
    if not combined_event:
        return None
    return process_event_payload(combined_event)


@shared_task(queue='bitbot')
def event_processor(data):
    batch_delay = get_batch_delay(data)
    if batch_delay > 0 and should_buffer_event(data, batch_delay=batch_delay):
        return enqueue_buffered_event(data, batch_delay)
    return process_event_payload(data)


def process_event_payload(data):
    command_id = None
    message_id = None
    bot_id = None
    dialog_id = None
    chatbot = None
    try:
        member_id = data.get("auth[member_id]")
        application_token = get_application_token(data)
        auth_access_token = data.get("auth[access_token]")
        
        bot_id = get_bot_id(data)
        
        # Extract bot-specific auth credentials
        bot_access_token = None
        if bot_id:
            bot_access_token = data.get(f"data[BOT][{bot_id}][AUTH][access_token]")
        
        inputs = extract_values(data, [
            'event', 'scope', 'client_endpoint', 'DIALOG_ID', 'AUTHOR_ID',
            'USER_ID', 'CHAT_AUTHOR_ID', 'IS_BOT', 'IS_CONNECTOR', 'IS_NETWORK', 'IS_EXTRANET',
            'MESSAGE_ID', 'MESSAGE', 'COMMAND_ID', 'COMMAND', 'COMMAND_PARAMS', 'CHAT_TITLE',
            'LANGUAGE', 'CHAT_ENTITY_DATA_1', 'CHAT_ENTITY_DATA_2', 'CHAT_ENTITY_ID',
            'FIRST_NAME', 'LAST_NAME', 'BOT_ID', 'CHAT_ID', 'MESSAGE_BATCH', 'MESSAGE_BATCH_SIZE',
        ])
        
        files = extract_files(data)
        if files:
            # Pass the first file ID and its type as variables
            inputs['file_id'] = files[0]['id']
            if 'viewerType' in files[0]:
                inputs['file_type'] = files[0]['viewerType']

        event = inputs.get("event")
        dialog_id = inputs.get("DIALOG_ID")
        command_id = inputs.get("COMMAND_ID")
        command = inputs.get("COMMAND")
        message_id = inputs.get("MESSAGE_ID")
        query = inputs.get("MESSAGE") or event

        if not bot_id or not application_token:
            raise ValueError("Missing ID or token")

        chatbot = get_chatbot(data, bot_id=bot_id, application_token=application_token)
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

        if auth_access_token:
            inputs['access_token'] = auth_access_token

        if bot_access_token:
            inputs['bot_access_token'] = bot_access_token

        connector = chatbot.connector
        provider = connector.provider
        base_url = connector.url or provider.url

        redis_key = f"bitbot:{member_id}:{bot_id}:{dialog_id}"
        session_id = redis_client.get(redis_key)

        def save_and_return_answer(answer, new_session_id=None):
            if new_session_id:
                redis_client.set(redis_key, new_session_id, ex=259200)
            if not answer:
                return None
            if command:
                return send_command_answer(chatbot, command_id, message_id, answer)
            return send_message(chatbot, bot_id, dialog_id, answer)

        # DIFy Chatflow
        if provider.type == "dify_chatflow":
            chat_client = ChatClient(connector.key, base_url)

            def raise_chat_error(exc, response_text=None):
                message = f"{exc}. query={query!r}. inputs={inputs}"
                if response_text:
                    message = f"{message}. response={response_text}"
                if isinstance(exc, ValidationError):
                    raise ValidationError(message) from exc
                if isinstance(exc, DifyClientError):
                    raise DifyClientError(
                        message,
                        getattr(exc, "status_code", None),
                        getattr(exc, "response", None),
                    ) from exc
                raise requests.HTTPError(message) from exc

            # If message is empty but file is present, send file name as message
            if not query and inputs.get('file_id'):
                files = extract_files(data)
                if files:
                    file_name = files[0].get('name', 'File')
                    query = f"File sent: {file_name}"
            kwargs = dict(
                inputs=inputs,
                query=query,
                user=dialog_id,
                response_mode="streaming",
            )
            used_existing_session = False
            if session_id:
                kwargs["conversation_id"] = session_id.decode()
                used_existing_session = True
            try:
                chat_response = chat_client.create_chat_message(**kwargs)
                chat_response.raise_for_status()
            except ValidationError as exc:
                raise_chat_error(exc)
            except DifyClientError as exc:
                if used_existing_session and "Conversation Not Exists" in str(exc):
                    redis_client.delete(redis_key)
                    retry_kwargs = dict(kwargs)
                    retry_kwargs.pop("conversation_id", None)
                    try:
                        chat_response = chat_client.create_chat_message(**retry_kwargs)
                        chat_response.raise_for_status()
                        used_existing_session = False
                    except ValidationError as retry_exc:
                        raise_chat_error(retry_exc)
                    except DifyClientError as retry_exc:
                        raise_chat_error(retry_exc)
                    except requests.HTTPError as retry_exc:
                        raise_chat_error(retry_exc, chat_response.text)
                else:
                    raise_chat_error(exc)
            except requests.HTTPError as exc:
                raise_chat_error(exc, chat_response.text)
            answer_parts = []
            outputs = {}
            conversation_id = kwargs.get("conversation_id")
            for line in chat_response.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                if not line or not line.startswith("data:"):
                    continue
                event_data = line[5:].strip()
                if event_data == "[DONE]":
                    continue
                try:
                    event = json.loads(event_data)
                except ValueError:
                    continue
                if event.get("conversation_id"):
                    conversation_id = event["conversation_id"]
                if event.get("event") in {"message", "agent_message", "message_replace"}:
                    text = event.get("answer") or event.get("data", {}).get("text")
                    if text:
                        if event.get("event") == "message_replace":
                            answer_parts = [text]
                        else:
                            answer_parts.append(text)
                if event.get("event") in {"node_finished", "workflow_finished"}:
                    event_outputs = event.get("data", {}).get("outputs")
                    if isinstance(event_outputs, dict):
                        outputs = event_outputs
            answer = "".join(answer_parts).strip()
            if not answer and outputs:
                answer = "\n".join(
                    f"{k}: {v}" for k, v in outputs.items()
                    if v not in (None, "", [], {})
                )
            return save_and_return_answer(
                answer,
                conversation_id if not used_existing_session else None,
            )

        # DIFy Workflow
        if provider.type == "dify_workflow":
            workflow_client = WorkflowClient(connector.key, base_url)
            response = workflow_client.run(
                inputs=inputs, user=dialog_id, response_mode="blocking"
            )
            response.raise_for_status()
            try:
                response_json = response.json()
            except ValueError:
                return save_and_return_answer(f"{response.text} {inputs}")

            outputs = response_json.get("data", {}).get("outputs", {})
            if not outputs:
                return

            answer = "\n".join(
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
        send_message(chatbot, bot_id, dialog_id, error_msg, system="Y")
        raise

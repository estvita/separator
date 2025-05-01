import requests
import redis
from celery import shared_task
from rest_framework.response import Response
from django.core.mail import send_mail
from django.conf import settings
import openai
from openai import OpenAI
import re
import json
from thoth.bot.models import Bot
import thoth.chatwoot.utils as chatwoot

from thoth.bitrix.utils import bitrix_user_add
from thoth.chatwoot.utils import ChatwootClient

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


@shared_task(bind=True, max_retries=5, default_retry_delay=5)
def message_processing(self, thread_id, redis_key, bot_id, role, content, conversation_status,
                       message_type, labels, account_id, conversation_id, sender_id):
    try:
        bot = Bot.objects.get(id=bot_id)
    except Bot.DoesNotExist:
        return "Bot not found"

    # Extract message details
    client = OpenAI(api_key=bot.token.key)

    if not thread_id:
        thread = client.beta.threads.create()
        thread_id = thread.id
        redis_client.set(redis_key, thread_id)

    try:
        message = client.beta.threads.messages.create(
            thread_id=thread_id,
            role=role,
            content=content
        )
    except openai.OpenAIError as exc:
        raise self.retry(exc=exc)

    if conversation_status == "open" or message_type != "incoming" or "blocked" in labels:
        return
    
    try:
        run = client.beta.threads.runs.create_and_poll(
            thread_id=thread_id,
            assistant_id=bot.assistant_id,
            instructions=bot.system_message
        )
    except openai.OpenAIError as exc:
        raise self.retry(exc=exc)

    ai_response = None
    chatwoot_client = ChatwootClient(account_id=account_id)
    conv_url = f"api/v1/accounts/{account_id}/conversations/{conversation_id}"

    if run.status == 'completed':
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            before=message.id,
        )
        raw_text = messages.data[0].content[0].text.value
        ai_response = re.sub(r"【.*?】", "", raw_text).strip()

    elif run.status == 'failed' and conversation_status != "open":
        resp = chatwoot.bot_handoff(f"{conv_url}/toggle_status", bot.agent_bot.token)
        ai_response = f"Извините, возникла ошибка: {run.last_error.code}. {resp}"

        send_mail(
            subject=f"Ошибка бота: {run.last_error.code}",
            message=run.last_error.message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[bot.owner],
            fail_silently=False,
        )

    elif run.status == 'requires_action':
        tool_outputs = []
        resp = "No response"

        if run.required_action:
            tool_calls = run.required_action.submit_tool_outputs.tool_calls

            for tool in tool_calls:
                func = tool.function.name
                if func == "collect_user_data":
                    user_data = json.loads(tool.function.arguments)
                    try:
                        upd = chatwoot_client.updtae_contact(sender_id, user_data)
                        upd.raise_for_status()
                        resp = f"Update successful"
                    except requests.RequestException as e:
                        resp = f"Error update_contact: {e.response.status_code} - {e.response.text}"

                elif func == "bot_handoff":
                    resp = chatwoot.bot_handoff(f"{conv_url}/toggle_status", bot.agent_bot.token)

                elif func == "remove_label":
                    resp = chatwoot_client.remove_conversation_label(conversation_id, bot.follow_up)

                elif func == "bitrix_user_add":
                    if not bot.bitrix:
                        resp = "error: bitrix not connected to bot"
                    else:
                        arguments = json.loads(tool.function.arguments)
                        email = arguments.get('email')
                        resp = bitrix_user_add(bot, email, account_id, conversation_id, sender_id)

                tool_outputs.append({
                    "tool_call_id": tool.id,
                    "output": resp
                })

            if tool_outputs:
                try:
                    run = client.beta.threads.runs.submit_tool_outputs_and_poll(
                        thread_id=thread_id,
                        run_id=run.id,
                        tool_outputs=tool_outputs
                    )
                except Exception as e:
                    print("Failed to submit tool outputs:", e)
            else:
                resp = "No tool outputs to submit"

            if run.status == 'completed':
                messages = client.beta.threads.messages.list(
                    thread_id=thread_id,
                    before=message.id,
                )
                ai_response = messages.data[0].content[0].text.value
            else:
                ai_response = f"same error: {run.status}"

    else:
        print(f"This status is not processed: {run.status}.")
    
    if ai_response:

        # Prepare response payload
        payload = {
            "content": ai_response,
            "message_type": "outgoing"
        }

        # Send the response back to the chat
        msg_url = f"{conv_url}/messages"
        try:
            resp = chatwoot.call_api(msg_url, data=payload, access_token=bot.agent_bot.token)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Error sending response to chat: {e}")
            return Response("Error sending the message.", status=500)


@shared_task(bind=True)
def manage_sip_user(self, action, bot_id, password=None):
    from opensipscli import cli
    from opensipscli.args import OpenSIPSCLIArgs
    command_list = ["user", action, f"{bot_id}@voice.thoth.kz", password]
    my_args = OpenSIPSCLIArgs(command=command_list)
    opensipscli = cli.OpenSIPSCLI(options=my_args)
    ret_code = opensipscli.cmdloop()
    if ret_code is not True:
        raise RuntimeError(f"OpenSIPSCLI code {ret_code}")
    return True
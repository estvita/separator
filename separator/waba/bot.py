import redis
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from separator.users.models import Message

from separator.waba.models import Phone, Bot, Ctwa
from separator.waba.tasks import send_ctwa_conversion

redis_client = redis.StrictRedis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


def _message_text(code, default=""):
    message = Message.objects.filter(code=code).first()
    if isinstance(message, Message):
        return message.message or default
    return default


def _send_bot_text(waba_bot, user_phone, body):
    from separator.waba.utils import call_api

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": user_phone,
        "type": "text",
        "text": {"body": body or " "},
    }
    endpoint = f"{waba_bot.phone.phone_id}/messages"
    return call_api(waba=waba_bot.phone.waba, endpoint=endpoint, payload=payload, method="post")


def _send_template_text(waba_bot, user_phone, code, default=""):
    return _send_bot_text(waba_bot, user_phone, _message_text(code, default))


@shared_task(queue='default')
def bot_processor(data, bot_id):
    waba_bot = Bot.objects.filter(id=bot_id).first()
    if not waba_bot:
        raise Exception(f"Bot {bot_id} not found")
    
    entry = data["entry"][0]
    changes = entry["changes"][0]
    value = changes.get('value', {})
    messages = value.get("messages", [])
    value_contacts = value.get("contacts", [])
    responses = []

    for message in messages:
        user_wa_id = None
        user_id = None
        if value_contacts:
            user_wa_id = value_contacts[0].get("wa_id")
            user_id = value_contacts[0].get("user_id")

        user_phone = user_wa_id or user_id
        if not user_phone:
            continue

        message_type = message.get("type")
        client_phone = Phone.objects.filter(phone=user_wa_id).first() if user_wa_id else None

        if not client_phone:
            responses.append(_send_template_text(
                waba_bot,
                user_phone,
                "waba_bot_connect",
                "This number is not connected to the bot.",
            ))
            continue

        if client_phone.date_end and timezone.now() > client_phone.date_end:
            responses.append(_send_template_text(
                waba_bot,
                user_phone,
                "waba_bot_expired",
                "The subscription for this number has expired.",
            ))
            continue

        if message_type == "contacts":
            found_open_ctwa = False
            option_number = 1
            contacts = message.get("contacts", [])
            for contact in contacts:
                phones = contact.get("phones", [])
                for phone in phones:
                    wa_id = phone.get("wa_id")
                    if not wa_id or not client_phone.waba_id:
                        continue

                    ctwa_objects = Ctwa.objects.filter(
                        phone=wa_id,
                        waba=client_phone.waba,
                        events__isnull=True,
                    ).distinct()
                    for ctwa in ctwa_objects:
                        found_open_ctwa = True
                        reply_hint = _message_text(
                            "waba_bot_instructions",
                            "Reply with + for conversion or - for cancellation.",
                        )
                        post_link = ctwa.source_url or "Post link is unavailable."
                        body = f"{option_number}. {post_link}\n\n{reply_hint}"
                        resp = _send_bot_text(waba_bot, user_phone, body)
                        responses.append(resp)
                        if resp:
                            resp_messages = resp.get("messages", [])
                            for resp_message in resp_messages:
                                resp_message_id = resp_message.get("id")
                                if resp_message_id:
                                    redis_client.set(f"ctwa:{resp_message_id}", str(ctwa.id), ex=604800)
                        option_number += 1

            if not found_open_ctwa:
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_no_ctwa",
                    "There are no open CTWA leads for this contact.",
                ))
            continue

        if message_type == "text":
            context = message.get("context", {})
            msg_body = (message.get("text", {}) or {}).get("body", "").strip()

            if msg_body not in ["+", "-"]:
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_usuported",
                    "Only contact messages and replies with + or - to a bot message are supported.",
                ))
                continue

            if not context:
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_usuported",
                    "Only contact messages and replies with + or - to a bot message are supported.",
                ))
                continue

            context_message_id = context.get("id")
            if not context_message_id:
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_context_missing",
                    "Could not identify the lead from the selected message.",
                ))
                continue

            ctwa_id = redis_client.get(f"ctwa:{context_message_id}")
            if isinstance(ctwa_id, bytes):
                ctwa_id = ctwa_id.decode("utf-8")

            if not ctwa_id:
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_ctwa_expired",
                    "The lead was not found or the selection has expired.",
                ))
                continue

            ctwa = Ctwa.objects.filter(id=ctwa_id).first()
            if not ctwa:
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_ctwa_not_found",
                    "The lead was not found.",
                ))
                continue

            if ctwa.events.exists():
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_ctwa_processed",
                    "This lead has already been processed.",
                ))
                continue

            if msg_body == "+":
                try:
                    conversion_result = send_ctwa_conversion.run(str(ctwa.id))
                    if not conversion_result:
                        raise Exception("Facebook did not return a conversion response")
                except Exception:
                    responses.append(_send_template_text(
                        waba_bot,
                        user_phone,
                        "waba_bot_conversion_error",
                        "Could not send the conversion.",
                    ))
                    raise
                redis_client.delete(f"ctwa:{context_message_id}")
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_conversion_sent",
                    "The conversion has been sent.",
                ))
            elif msg_body == "-":
                try:
                    conversion_result = send_ctwa_conversion.run(str(ctwa.id), event="OrderCanceled")
                    if not conversion_result:
                        raise Exception("Facebook did not return a conversion response")
                except Exception:
                    responses.append(_send_template_text(
                        waba_bot,
                        user_phone,
                        "waba_bot_conversion_error",
                        "Could not send the conversion.",
                    ))
                    raise
                redis_client.delete(f"ctwa:{context_message_id}")
                responses.append(_send_template_text(
                    waba_bot,
                    user_phone,
                    "waba_bot_conversion_sent",
                    "The conversion has been sent.",
                ))
            continue

        responses.append(_send_template_text(
            waba_bot,
            user_phone,
            "waba_bot_usuported",
            "Only contact messages and replies with + or - to a bot message are supported.",
        ))

    return responses

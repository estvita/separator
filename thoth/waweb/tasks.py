import re
import requests
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from celery import shared_task
import thoth.waweb.utils as utils
from thoth.waweb.models import Session


@shared_task(queue='waweb')
def send_message(session_id, recipient, content, cont_type="string"):
    try:
        session = Session.objects.get(session=session_id)
        server = session.server
        headers = {"apikey": session.apikey}
        cleaned = re.sub(r'\D', '', recipient)
        if cont_type == "string":
            payload = {
                "number": cleaned,
                "text": content,
                "linkPreview": True,
            }
            url = f"{server.url}message/sendText/{session_id}"
            resp = requests.post(url, json=payload, headers=headers)
        elif cont_type == "media":
            url = f"{server.url}message/sendMedia/{session_id}"
            mimetype = content.get("mimetype", "")
            base_type = mimetype.split('/')[0]
            mediatype = base_type if base_type in ["image"] else "document"
            payload = {
                "number": cleaned,
                "mediatype": mediatype,
                "mimetype": content.get("mimetype"),
                "media": content.get("data"),
                "fileName": content.get("filename")
            }
            resp = requests.post(url, json=payload, headers=headers)
        else:
            raise Exception("Unknown cont_type")

        if resp and resp.status_code == 201:
            utils.store_msg(resp)
            return resp.json()
        else:
            raise Exception(f"Request failed: {resp.status_code}, {resp.text}")

    except Exception as e:
        raise


@shared_task(queue='waweb')
def send_message_task(session_id, recipients, content, cont_type="string"):
    if cont_type == "media":
        content = utils.download_file(content)
    for recipient in recipients:
        send_message.delay(session_id, recipient, content, cont_type)


@shared_task(queue='waweb')
def delete_sessions(days):
    now = timezone.now()
    filters = Q((Q(phone__isnull=True) | Q(phone='')) & Q(date_end__lt=now))
    if days is not None:
        try:
            days_int = int(days)
            date_limit = now - timedelta(days=days_int)
            filters = filters | Q(date_end__lt=date_limit)
        except (TypeError, ValueError):
            pass

    sessions = Session.objects.filter(filters)
    for session in sessions:
        server = session.server
        headers = {"apikey": server.api_key}
        url = f"{server.url}instance/delete/{session.session}"
        requests.delete(url, headers=headers)
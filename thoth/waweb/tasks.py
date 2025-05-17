import requests
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from celery import shared_task
import thoth.waweb.utils as utils
from django.conf import settings
from thoth.waweb.models import WaServer, WaSession

WABWEB_SRV = settings.WABWEB_SRV

@shared_task
def send_message_task(session_id, recipients, content, cont_type="string", from_web=False):
    if cont_type == "media":
        content = utils.download_file(content)
    for recipient in recipients:
        resp = utils.send_message(session_id, recipient, content, cont_type)
        if resp.status_code == 201 and not from_web:
            utils.store_msg(resp)


@shared_task
def delete_sessions(days=None):
    now = timezone.now()
    filters = Q((Q(phone__isnull=True) | Q(phone='')) & Q(date_end__lt=now))
    if days is not None:
        try:
            days_int = int(days)
            date_limit = now - timedelta(days=days_int)
            filters = filters | Q(date_end__lt=date_limit)
        except (TypeError, ValueError):
            pass

    sessions = WaSession.objects.filter(filters)
    wa_server = WaServer.objects.get(id=WABWEB_SRV)
    headers = {"apikey": wa_server.api_key}
    for session in sessions:
        url = f"{wa_server.url}instance/delete/{session.session}"
        requests.delete(url, headers=headers)
        session.delete()
import requests
from celery import shared_task
import thoth.waweb.utils as utils
from thoth.waweb.models import WaServer, WaSession


@shared_task
def send_message_task(session_id, recipients, content, cont_type="string", from_web=False):
    if cont_type == "media":
        content = utils.download_file(content)
    for recipient in recipients:
        resp = utils.send_message(session_id, recipient, content, cont_type)
        if resp.status_code == 201 and not from_web:
            utils.store_msg(resp)

@shared_task
def restart_sessions():
    wa_server = WaServer.objects.first()
    sessions = WaSession.objects.all()
    headers = {"x-api-key": wa_server.api_key}

    for session in sessions:
        url = f"{wa_server.url}session/restart/{session.session}"
        requests.get(url, headers=headers)


@shared_task(bind=True, max_retries=5, default_retry_delay=10)
def terminate_sessions(self):
    wa_server = WaServer.objects.first()
    headers = {"x-api-key": wa_server.api_key}
    try:
        resp = requests.get(f"{wa_server.url}session/terminateInactive", headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        self.retry(exc=exc)
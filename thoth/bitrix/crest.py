from urllib.parse import urlparse
from django.utils import timezone
import logging

import requests
from django.db import transaction
from django.conf import settings
from thoth.waweb.tasks import send_message_task
from thoth.users.models import Message

from .models import AppInstance, Credential

logger = logging.getLogger("django")


def call_method(appinstance: AppInstance, 
                b24_method: str, 
                data: dict, 
                attempted_refresh=False, 
                verify=True,
                admin=None):
    
    portal = appinstance.portal
    if admin is None:
        active_users = portal.users.filter(active=True)
    else:
        active_users = portal.users.filter(admin=admin, active=True)

    last_exc = None
    for b24_user in active_users:
        credential = b24_user.credentials.filter(app_instance=appinstance).first()
        if not credential:
            continue
        endpoint = f"{portal.protocol}://{portal.domain}/rest/"
        payload = {"auth": credential.access_token, **data}
        try:
            response = requests.post(f"{endpoint}{b24_method}", json=payload,
                                    allow_redirects=False, timeout=60, verify=verify)
            appinstance.status = response.status_code
        except requests.exceptions.SSLError:
            if verify:
                return call_method(appinstance, b24_method, data, attempted_refresh, verify=False)
            else:
                raise

        if response.status_code == 302 and not attempted_refresh:
            new_url = response.headers['Location']
            parsed_url = urlparse(new_url)
            portal = appinstance.portal
            domain = parsed_url.netloc
            if portal.domain != domain:
                portal.domain = domain
                portal.save()
            appinstance.attempts = 0
            appinstance.save()
            return call_method(appinstance, b24_method, data, attempted_refresh=True)

        elif response.status_code == 200:
            appinstance.attempts = 0
            appinstance.save()
            return response.json()

        else:
            if response.status_code == 401:
                resp = response.json()
                error = resp.get("error", "")
                error_description = resp.get("error_description", "")
                if "REST is available only on commercial plans" in error_description and not appinstance.portal.license_expired:
                    appinstance.portal.license_expired = True
                    appinstance.portal.save()
                    waweb_id = settings.WAWEB_SYTEM_ID
                    if waweb_id and appinstance.owner.phone_number:
                        try:
                            notification = Message.objects.get(code="b24_expired")
                            send_message_task.delay(waweb_id, [str(appinstance.owner.phone_number)], notification.message)
                        except Message.DoesNotExist:
                            pass
                    raise Exception("b24 license expired")
                
                if error == "expired_token" and not attempted_refresh:
                    refreshed = refresh_token(credential)
                    if refreshed:
                        return call_method(appinstance, b24_method, data, attempted_refresh=True)
                    else:
                        last_exc = Exception(f"Token refresh failed for user {b24_user.user_id} in portal {portal.domain}")
                        continue
                if error == "authorization_error":
                    b24_user.active = False
                    b24_user.save()
                    last_exc = Exception(f"Unauthorized error: {response.json()}")
                    continue
                last_exc = Exception(f"Unauthorized error: {response.json()}")
                continue

            last_exc = Exception(f"Failed to call bitrix: {appinstance.portal.domain} "
                            f"status {response.status_code}, response: {response.json()}")
            
    appinstance.attempts += 1
    appinstance.save()
    if last_exc:
        raise last_exc
    raise Exception("No active users for portal")


def refresh_token(credential: Credential):
    payload = {
        "grant_type": "refresh_token",
        "client_id": credential.app_instance.app.client_id,
        "client_secret": credential.app_instance.app.client_secret,
        "refresh_token": credential.refresh_token,
    }
    response = requests.post(f"{settings.BITRIX_OAUTH_URL}/oauth/token/", data=payload)
    try:
        response_data = response.json()
    except Exception:
        return False

    if response.status_code != 200:
        return False

    credential.access_token = response_data["access_token"]
    credential.refresh_token = response_data["refresh_token"]
    credential.refresh_date = timezone.now()
    with transaction.atomic():
        credential.save(update_fields=["access_token", "refresh_token", "refresh_date"])
    return True

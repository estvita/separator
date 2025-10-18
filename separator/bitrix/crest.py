import logging
import requests
from urllib.parse import urlparse
from django.utils import timezone

from django.db import transaction
from django.conf import settings

from .models import AppInstance, Credential, User

logger = logging.getLogger("django")


def call_method(appinstance: AppInstance, 
                b24_method: str, 
                data: dict=None, 
                attempted_refresh=False, 
                verify=True,
                admin=None,
                b24_user_id=None):
    if data is None:
        data = {}
    
    portal = appinstance.portal
    if b24_user_id:
        b24_user = User.objects.get(id=b24_user_id)
        b24_users = [b24_user]
    else:
        if admin is None:
            b24_users = portal.users.filter(active=True)
        else:
            b24_users = portal.users.filter(admin=admin, active=True)

    last_exc = None
    for b24_user in b24_users:
        credential = b24_user.credentials.filter(app_instance=appinstance).first()
        if not credential:
            continue
        endpoint = f"{portal.protocol}://{portal.domain}/rest/"
        payload = {"auth": credential.access_token, **data}
        try:
            response = requests.post(f"{endpoint}{b24_method}", json=payload,
                                    allow_redirects=False, verify=verify)
            appinstance.status = response.status_code
        except requests.exceptions.SSLError:
            if verify:
                return call_method(appinstance, b24_method, data, attempted_refresh, verify=False)
            else:
                raise

        if response.status_code == 302 and not attempted_refresh:
            new_url = response.headers['Location']
            parsed_url = urlparse(new_url)
            domain = parsed_url.netloc
            if portal.domain != domain:
                portal.domain = domain
                portal.save()
            appinstance.attempts = 0
            appinstance.save()
            return call_method(appinstance, b24_method, data, attempted_refresh=True)

        elif response.status_code == 200:
            if appinstance.attempts != 0:
                appinstance.attempts = 0
                appinstance.save()
            if portal.license_expired:
                portal.license_expired = False
                portal.save()
            return response.json()

        else:
            if response.status_code == 401:
                resp = response.json()
                error = resp.get("error", "")
                if error == "ACCESS_DENIED" and not portal.license_expired:
                    portal.license_expired = True
                    portal.save()                
                elif error == "expired_token" and not attempted_refresh:
                    refreshed = refresh_token(credential)
                    if refreshed:
                        return call_method(appinstance, b24_method, data, attempted_refresh=True)
                    else:
                        last_exc = Exception(f"Token refresh failed for user {b24_user.user_id} in portal {portal.domain}")
                        continue
                elif error == "authorization_error":
                    b24_user.active = False
                    b24_user.save()
                    last_exc = Exception(f"Unauthorized error: instance {appinstance.id} {response.json()}")
                    continue
                last_exc = Exception(f"Unauthorized error: instance {appinstance.id} {response.json()}")
                continue

            last_exc = Exception(f"Failed to call bitrix: {appinstance.portal.domain} "
                            f"status {response.status_code}, response: {response.json()}")
            
    appinstance.attempts += 1
    appinstance.save()
    if last_exc:
        raise Exception(f"{last_exc} method: {b24_method} data:{data}")
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

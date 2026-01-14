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
                b24_user_id=None,
                timeout=30):
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
        
        refresh_attempted = False
        while True:
            endpoint = f"{portal.protocol}://{portal.domain}/rest"
            payload = {"auth": credential.access_token, **data}
            try:
                response = requests.post(f"{endpoint}/{b24_method}", json=payload,
                                        allow_redirects=False, verify=verify, timeout=timeout)
                if appinstance.status != response.status_code:
                    appinstance.status = response.status_code
                    appinstance.save()
            except requests.exceptions.Timeout:
                # If timeout occurs, we should probably stop trying for this user/portal this time
                # and let the caller handle the retry (e.g. Celery task)
                raise
            except requests.exceptions.SSLError:
                if verify:
                    return call_method(appinstance, b24_method, data, attempted_refresh, verify=False, timeout=timeout)
                else:
                    raise

            if response.status_code == 302 and not attempted_refresh:
                new_url = response.headers['Location']
                parsed_url = urlparse(new_url)
                domain = parsed_url.netloc
                if portal.domain != domain:
                    portal.domain = domain
                    portal.save()
                return call_method(appinstance, b24_method, data, attempted_refresh=True)

            elif response.status_code == 200:
                if portal.license_expired:
                    portal.license_expired = False
                    portal.save()
                return response.json()

            elif response.status_code == 401:
                resp = response.json()
                error = resp.get("error", "")
                if error == "ACCESS_DENIED" and not portal.license_expired:
                    portal.license_expired = True
                    portal.save()
                elif error == "expired_token" and not refresh_attempted:
                    refreshed = refresh_token(credential)
                    if refreshed:
                        refresh_attempted = True
                        continue
                    else:
                        last_exc = Exception(f"Token refresh failed for user {b24_user.user_id} in portal {portal.domain}")
                        break
                elif error == "authorization_error":
                    b24_user.active = False
                    b24_user.save()
                    last_exc = Exception(f"Unauthorized error: instance {appinstance.id} {response.json()}")
                    break
                last_exc = Exception(f"Unauthorized error: instance {appinstance.id} {response.json()}")
                break
            elif response.status_code == 403:
                try:
                    resp_data = response.json()
                    last_exc = Exception(f"Access error: instance {appinstance.id} {resp_data}")
                    break
                except ValueError:
                    last_exc = Exception(f"Access error: instance {appinstance.id} {response.text}")
                    portal.license_expired = True
                    portal.save()
                    break
            else:
                last_exc = Exception(f"Failed to call bitrix: {appinstance.portal.domain} "
                                f"status {response.status_code}, response: {response.text}")
                break
            
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
    try:
        response = requests.post(f"{settings.BITRIX_OAUTH_URL}/oauth/token/", data=payload, timeout=10)
    except requests.exceptions.RequestException:
        return False
        
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

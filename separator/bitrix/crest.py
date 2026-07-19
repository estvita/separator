import logging
import re
import requests
from urllib.parse import urlparse
from django.utils import timezone

from django.db import transaction
from django.conf import settings

from .models import AppInstance, Credential, User

logger = logging.getLogger("django")


class BitrixAccessDeniedError(Exception):
    pass


def _save_instance_status(appinstance, status):
    if appinstance.status == status:
        return
    appinstance.status = status
    try:
        appinstance.save(update_fields=["status"])
    except Exception:
        pass


def _connection_error_status(exc):
    match = re.search(r"\[Errno\s+(-?\d+)\]", str(exc))
    if match:
        return int(match.group(1))
    return -1


def _response_json(response, b24_method):
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"Invalid Bitrix JSON response for {b24_method}: "
            f"status {response.status_code}, response: {response.text[:500]}"
        ) from exc


def call_method(appinstance: AppInstance, 
                b24_method: str, 
                data: dict=None, 
                attempted_refresh=False, 
                verify=True,
                admin=True,
                b24_user_id=None,
                timeout=30):
    if data is None:
        data = {}
    
    portal = appinstance.portal
    if b24_user_id:
        b24_user = User.objects.get(id=b24_user_id)
        b24_users = [b24_user]
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
                _save_instance_status(appinstance, response.status_code)
            except requests.exceptions.Timeout:
                # If timeout occurs, we should probably stop trying for this user/portal this time
                # and let the caller handle the retry (e.g. Celery task)
                raise
            except requests.exceptions.SSLError:
                if verify:
                    return call_method(
                        appinstance,
                        b24_method,
                        data,
                        attempted_refresh,
                        verify=False,
                        admin=admin,
                        b24_user_id=b24_user_id,
                        timeout=timeout,
                    )
                else:
                    raise
            except requests.exceptions.ConnectionError as exc:
                _save_instance_status(appinstance, _connection_error_status(exc))
                raise

            if response.status_code == 302 and not attempted_refresh:
                new_url = response.headers['Location']
                parsed_url = urlparse(new_url)
                domain = parsed_url.netloc
                if portal.domain != domain:
                    portal.domain = domain
                    try:
                        portal.save()
                    except Exception:
                        pass
                return call_method(
                    appinstance,
                    b24_method,
                    data,
                    attempted_refresh=True,
                    verify=verify,
                    admin=admin,
                    b24_user_id=b24_user_id,
                    timeout=timeout,
                )

            elif response.status_code == 200:
                if portal.license_expired:
                    portal.license_expired = False
                    try:
                        portal.save()
                    except Exception:
                        pass
                return _response_json(response, b24_method)

            elif response.status_code == 401:
                resp = _response_json(response, b24_method)
                error = resp.get("error", "")
                if error == "ACCESS_DENIED":
                    if not portal.license_expired:
                        portal.license_expired = True
                        try:
                            portal.save()
                        except Exception:
                            pass
                    raise BitrixAccessDeniedError(response.text)
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
                    try:
                        b24_user.save()
                    except Exception:
                        pass
                
                last_exc = Exception(f"Unauthorized error: instance {appinstance.id} {resp}")
                break
            elif response.status_code == 403:
                try:
                    resp_data = _response_json(response, b24_method)
                    if resp_data.get("error") == "ACCESS_DENIED":
                        if not portal.license_expired:
                            portal.license_expired = True
                            try:
                                portal.save()
                            except Exception:
                                pass
                        raise BitrixAccessDeniedError(response.text)
                    last_exc = Exception(f"Access error: instance {appinstance.id} {resp_data}")
                    break
                except ValueError:
                    portal.license_expired = True
                    try:
                        portal.save()
                    except Exception:
                        pass
                    raise BitrixAccessDeniedError(response.text)
            else:
                last_exc = Exception(f"Failed to call bitrix: {appinstance.portal.domain} "
                                f"status {response.status_code}, response: {response.text}")
                break
            
    if last_exc:
        raise Exception(f"{last_exc} method: {b24_method} data:{data}")
    raise Exception("No active users for portal")


def refresh_token(credential: Credential, raise_request_exception=False):
    payload = {
        "grant_type": "refresh_token",
        "client_id": credential.app_instance.app.client_id,
        "client_secret": credential.app_instance.app.client_secret,
        "refresh_token": credential.refresh_token,
    }
    try:
        response = requests.post(f"{settings.BITRIX_OAUTH_URL}/oauth/token/", data=payload, timeout=10)
    except requests.exceptions.RequestException:
        if raise_request_exception:
            raise
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
    try:
        with transaction.atomic():
            credential.save(update_fields=["access_token", "refresh_token", "refresh_date"])
    except Exception:
        pass
    return True

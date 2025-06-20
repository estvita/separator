from urllib.parse import urlparse
import logging

import requests
from django.db import transaction

from .models import AppInstance

logger = logging.getLogger("django")


def call_method(appinstance: AppInstance, b24_method: str, data: dict, attempted_refresh=False, verify=True):
    endpoint = f"https://{appinstance.portal.domain}/rest/"
    access_token = appinstance.access_token

    payload = {"auth": access_token, **data}
    try:
        response = requests.post(f"{endpoint}{b24_method}", json=payload,
                                allow_redirects=False, timeout=60, verify=verify)
        appinstance.status = response.status_code
    except requests.exceptions.SSLError:
        if verify:
            response = call_method(appinstance, b24_method, data, attempted_refresh, verify=False)
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
        appinstance.attempts += 1
        appinstance.save()
        if response.status_code == 401:
            error = response.json().get("error", "")
            if error == "expired_token" and not attempted_refresh:
                refreshed = refresh_token(appinstance)
                if isinstance(refreshed, AppInstance):
                    return call_method(appinstance, b24_method, data, attempted_refresh=True)
                else:
                    raise Exception(f"Token refresh failed for portal {appinstance.portal.domain}")
            else:
                raise Exception(f"Unauthorized error: {response.json()}")

        raise Exception(f"Failed to call bitrix: {appinstance.portal.domain} "
                        f"status {response.status_code}, response: {response.json()}")


def refresh_token(appinstance: AppInstance):
    payload = {
        "grant_type": "refresh_token",
        "client_id": appinstance.app.client_id,
        "client_secret": appinstance.app.client_secret,
        "refresh_token": appinstance.refresh_token,
    }
    response = requests.post("https://oauth.bitrix.info/oauth/token/", data=payload)
    try:
        response_data = response.json()
    except Exception:
        raise Exception(f"Invalid response while refreshing token for portal {appinstance.portal.domain}")

    if response.status_code != 200:
        raise Exception(f"Failed to refresh token: {appinstance.portal.domain} {response_data}")

    appinstance.access_token = response_data["access_token"]
    appinstance.refresh_token = response_data["refresh_token"]
    with transaction.atomic():
        appinstance.save(update_fields=["access_token", "refresh_token"])
    return appinstance

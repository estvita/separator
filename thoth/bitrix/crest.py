import logging
from urllib.parse import urlparse

import requests
from django.db import transaction
from django.http import JsonResponse

from .models import AppInstance

logger = logging.getLogger("django")


def call_method(appinstance: AppInstance, b24_method: str, data: dict, attempted_refresh=False):
    endpoint = appinstance.portal.client_endpoint
    access_token = appinstance.access_token

    try:
        payload = {"auth": access_token, **data}
        logger.debug(f"Data sent to b24: {payload}")
        response = requests.post(f"{endpoint}{b24_method}", json=payload, allow_redirects=False)
        appinstance.status = response.status_code

        if response.status_code == 302 and not attempted_refresh:
            new_url = response.headers['Location']
            parsed_url = urlparse(new_url)
            
            portal = appinstance.portal
            domain = parsed_url.netloc
            
            if portal.domain != domain:
                portal.domain = domain
                portal.client_endpoint = f"https://{domain}/rest/"
                portal.save()
                appinstance.attempts = 0
                appinstance.save()

                return call_method(appinstance, b24_method, data, attempted_refresh=True)
        
        elif response.status_code == 200:
            appinstance.attempts = 0
        else:
            appinstance.attempts += 1

        appinstance.save()

        if response.status_code == 401:
            if response.json().get("error") == "expired_token" and not attempted_refresh:
                if refresh_token(appinstance):
                    # Try the method call again with the new token
                    return call_method(appinstance, b24_method, data, attempted_refresh=True)
                else:
                    logger.error(f"Token refresh failed. portal {appinstance.portal.domain} {response.json()}")
                    return JsonResponse({"detail": "Token refresh failed, aborting."}, status=500)
        
        logger.debug(f"Request ended: {response} {response.json()}")
        return response.json()

    except (requests.HTTPError, Exception) as e:
        logger.error(f"portal error crest {appinstance.portal.domain}: {e}")
        return JsonResponse({"detail": str(e)}, status=500)


def refresh_token(appinstance: AppInstance):
    payload = {
        "grant_type": "refresh_token",
        "client_id": appinstance.app.client_id,
        "client_secret": appinstance.app.client_secret,
        "refresh_token": appinstance.refresh_token,
    }
    try:
        response = requests.post("https://oauth.bitrix.info/oauth/token/", data=payload)
        response_data = response.json()

        if response.status_code != 200:
            raise Exception(f"Failed to refresh token: {appinstance.portal.domain} {response_data}")

        appinstance.access_token = response_data["access_token"]
        appinstance.refresh_token = response_data["refresh_token"]

        with transaction.atomic():
            appinstance.save()

        return appinstance
    except Exception as e:
        logger.error(
            f"Error refreshing token: {e}",
        )
        return JsonResponse({"detail": str(e)}, status=500)

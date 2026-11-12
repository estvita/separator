import json
import time
import uuid
from urllib.parse import urlsplit

import redis
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.urls import Resolver404, resolve, reverse
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from .models import App, AppInstance, Bitrix, User as BitrixUser


LINK_TTL = 24 * 60 * 60
SESSION_LINKS = "bitrix_link_requests"
redis_client = redis.StrictRedis.from_url(
    settings.REDIS_URL, socket_connect_timeout=5, socket_timeout=5,
)


class LinkError(Exception):
    pass


def _key(token):
    return f"bitrix:link:{token}"


def _signup_key(user_id):
    return f"bitrix:link:signup:{user_id}"


def create_link(request, app, b24_user):
    token = uuid.uuid4().hex
    expires_at = int(time.time()) + LINK_TTL
    data = {
        "app_id": str(app.pk),
        "portal_id": b24_user.bitrix_id,
        "b24_user_id": b24_user.pk,
        "expires_at": expires_at,
    }
    redis_client.set(_key(token), json.dumps(data), ex=LINK_TTL)
    links = {
        key: expiry for key, expiry in request.session.get(SESSION_LINKS, {}).items()
        if expiry > time.time()
    }
    links[token] = expires_at
    request.session[SESSION_LINKS] = links
    messages.info(request, format_html(
        _("Link Bitrix24 user {user_id} on {portal} to your Separator account."),
        user_id=b24_user.user_id, portal=b24_user.bitrix.domain,
    ))
    return reverse("bitrix_link", kwargs={"token": token})


def get_link(token, request=None):
    if request is not None and request.session.get(SESSION_LINKS, {}).get(token, 0) <= time.time():
        raise LinkError(_("The linking session has expired. Open the application in Bitrix24 again."))
    raw = redis_client.get(_key(token))
    data = json.loads(raw) if raw else None
    if not data or data["expires_at"] <= time.time():
        raise LinkError(_("The linking request has expired. Open the application in Bitrix24 again."))
    return data


def auth_link_token(request, redirect_url=None):
    url = redirect_url or request.POST.get("next") or request.GET.get("next")
    if not url or not url_has_allowed_host_and_scheme(url, {request.get_host()}):
        return None
    try:
        match = resolve(urlsplit(url).path)
    except Resolver404:
        return None
    if match.url_name != "bitrix_link":
        return None
    token = match.kwargs["token"]
    if request.session.get(SESSION_LINKS, {}).get(token, 0) <= time.time():
        return None
    return token


def register_signup(request, user):
    token = auth_link_token(request)
    if not token:
        return
    data = get_link(token, request)
    if data.get("signup_user_id"):
        raise LinkError(_("This linking request already belongs to another registration."))
    address = EmailAddress.objects.get(user=user, email__iexact=user.email)
    data["signup_user_id"] = user.pk
    ttl = int(data["expires_at"] - time.time())
    if ttl <= 0 or not redis_client.set(_key(token), json.dumps(data), ex=ttl, xx=True):
        raise LinkError(_("The linking request has expired. Open the application in Bitrix24 again."))
    redis_client.set(_signup_key(user.pk), token, ex=ttl)
    # The Bitrix24 registration flow also requires confirmation on a local
    # installation where normal account registration may not require it.
    if settings.ACCOUNT_EMAIL_VERIFICATION == "none" and not address.verified:
        address.send_confirmation(request, signup=True)


def confirm_reauthentication(request, user):
    token = auth_link_token(request)
    if not token:
        return
    data = get_link(token, request)
    data["confirmed_user_id"] = user.pk
    if not redis_client.set(_key(token), json.dumps(data), xx=True, keepttl=True):
        raise LinkError(_("The linking request has expired. Open the application in Bitrix24 again."))


def complete_link(token, user):
    data = get_link(token)
    if not user.is_active:
        raise LinkError(_("This account is inactive."))
    signup_user_id = data.get("signup_user_id")
    if signup_user_id:
        if signup_user_id != user.pk:
            raise LinkError(_("Sign in with the account used for this registration."))
        if not EmailAddress.objects.filter(
            user=user, verified=True,
        ).exists():
            raise LinkError(_("Confirm your email address to finish linking Bitrix24."))

    with transaction.atomic():
        app = App.objects.filter(pk=data["app_id"], autologin=True).first()
        b24_user = BitrixUser.objects.select_for_update().filter(
            pk=data["b24_user_id"], bitrix_id=data["portal_id"], active=True,
        ).first()
        if not app or not b24_user:
            raise LinkError(_("This Bitrix24 linking request is no longer available."))
        if data["expires_at"] <= time.time():
            raise LinkError(_("The linking request has expired. Open the application in Bitrix24 again."))
        if b24_user.owner_id and b24_user.owner_id != user.pk:
            raise LinkError(_("This Bitrix24 user is already linked to another account."))
        if not b24_user.owner_id:
            b24_user.owner = user
            b24_user.save(update_fields=["owner"])
        Bitrix.objects.filter(pk=data["portal_id"], owner__isnull=True).update(owner=user)
        AppInstance.objects.filter(portal_id=data["portal_id"], owner__isnull=True).update(owner=user)

    redis_client.delete(_key(token))
    if signup_user_id:
        redis_client.delete(_signup_key(signup_user_id))
    return app, b24_user.bitrix


def prepare_app_open(request, app, user, installed_app=None):
    installed_app = installed_app or request.session.pop("installed_app", None)
    if installed_app and user and "separator.home" in settings.INSTALLED_APPS:
        if user.phone_number and not user.integrator:
            from separator.home.tasks import prepare_lead

            prepare_lead.delay(user.id, f"App installed: {app.name}")
        else:
            request.session["installed_app"] = installed_app


def finish_link(request, token, user, app, portal):
    links = request.session.get(SESSION_LINKS, {})
    links.pop(token, None)
    request.session[SESSION_LINKS] = links
    request.session["b24_data"] = {"member_id": portal.member_id}
    prepare_app_open(request, app, user)
    return app.page_url


def link_after_login(request, user, response):
    # Allauth can log in immediately after confirming email in this request.
    confirmed = getattr(request, "_bitrix_confirmed_link", None)
    if confirmed and confirmed[1] == user.pk:
        token, _, app, portal = confirmed
    else:
        token = auth_link_token(request, response.get("Location"))
        if not token:
            return
        app, portal = complete_link(token, user)
    response["Location"] = finish_link(request, token, user, app, portal)


def link_after_email_confirmation(request, email_address):
    raw = redis_client.get(_signup_key(email_address.user_id))
    if not raw:
        return
    token = raw.decode()
    data = get_link(token)
    if (
        data.get("signup_user_id") != email_address.user_id
        or not email_address.verified
    ):
        return
    app, portal = complete_link(token, email_address.user)
    request._bitrix_confirmed_link = (token, email_address.user_id, app, portal)
    messages.success(request, _("Bitrix24 account linked. Open the application in Bitrix24 to continue."))

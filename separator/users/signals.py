import logging

from allauth.account.signals import authentication_step_completed, email_confirmed, user_logged_in, user_signed_up
from django.contrib import messages
from django.dispatch import receiver
from redis.exceptions import RedisError
from rest_framework.authtoken.models import Token

from separator.bitrix import auth as bitrix_auth
from separator.users.tasks import get_site

logger = logging.getLogger("django")

@receiver(user_logged_in)
def set_user_site(sender, request, user, **kwargs):
    site = get_site(request)
    if site and not user.site:
        user.site = site
        user.save()

@receiver(email_confirmed)
def email_confirmed_handler(request, email_address, **kwargs):
    user = email_address.user

    # Создаём токен после подтверждения почты
    Token.objects.get_or_create(user=user)

    try:
        bitrix_auth.link_after_email_confirmation(request, email_address)
    except bitrix_auth.LinkError:
        # Confirmation remains valid even if the optional linking request expired.
        pass
    except RedisError:
        logger.warning("Redis unavailable while completing Bitrix24 account linking")


@receiver(user_signed_up)
def remember_bitrix_signup(sender, request, user, **kwargs):
    try:
        bitrix_auth.register_signup(request, user)
    except bitrix_auth.LinkError:
        pass
    except RedisError:
        logger.warning("Redis unavailable while retaining Bitrix24 registration context")


@receiver(user_logged_in)
def complete_bitrix_login(sender, request, user, response, **kwargs):
    try:
        bitrix_auth.link_after_login(request, user, response)
    except bitrix_auth.LinkError as exc:
        messages.warning(request, str(exc))
    except RedisError:
        logger.warning("Redis unavailable while completing Bitrix24 account linking")


@receiver(authentication_step_completed)
def confirm_bitrix_reauthentication(sender, request, user, reauthenticated=False, **kwargs):
    if not reauthenticated or not request.user.is_authenticated or request.user.pk != user.pk:
        return
    try:
        bitrix_auth.confirm_reauthentication(request, user)
    except bitrix_auth.LinkError as exc:
        messages.warning(request, str(exc))
    except RedisError:
        logger.warning("Redis unavailable while confirming Bitrix24 account linking")

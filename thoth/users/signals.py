from allauth.account.signals import email_confirmed, email_confirmation_sent
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from django.conf import settings

from thoth.users.tasks import create_user_task

from .models import Notifications
from thoth.waweb.tasks import send_message_task

User = get_user_model()


@receiver(email_confirmation_sent)
def welcome_message(request, confirmation, **kwargs):
    user = confirmation.email_address.user
    phone_number = getattr(user, "phone_number", None)
    try:
        notification = Notifications.objects.get(code="welcome")
    except Notifications.DoesNotExist:
        notification = None

    waweb_id = settings.WAWEB_SYTEM_ID
    if notification and phone_number and waweb_id and notification.message:
        send_message_task.delay(waweb_id, [str(phone_number)], notification.message)


@receiver(email_confirmed)
def email_confirmed_handler(request, email_address, **kwargs):
    email = email_address.email
    user = email_address.user

    # Создаём токен после подтверждения почты
    Token.objects.get_or_create(user=user)
    if settings.CHATWOOT_ENABLED:
        create_user_task.delay(email, user.id)
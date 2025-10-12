from allauth.account.signals import email_confirmed, email_confirmation_sent
from django.dispatch import receiver
from rest_framework.authtoken.models import Token
from django.conf import settings

from separator.users.tasks import create_user_task
from separator.waweb.tasks import send_message_task

from .models import Message


@receiver(email_confirmation_sent)
def welcome_message(request, confirmation, **kwargs):
    user = confirmation.email_address.user
    phone_number = getattr(user, "phone_number", None)
    try:
        notification = Message.objects.get(code="welcome")
    except Message.DoesNotExist:
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
        from separator.chatwoot.models import User
        try:
            chatwoot_user = User.objects.filter(owner=user).first()
            if not chatwoot_user:
                create_user_task.delay(email, user.id)
        except Exception as e:
            print("chatwoot_user error", e)
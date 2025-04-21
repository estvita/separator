from allauth.account.signals import email_confirmed
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from django.conf import settings

from thoth.users.tasks import create_user_task

User = get_user_model()

@receiver(email_confirmed)
def email_confirmed_handler(request, email_address, **kwargs):
    email = email_address.email
    user = email_address.user

    # Создаём токен после подтверждения почты
    Token.objects.get_or_create(user=user)
    if settings.CHATWOOT_ENABLED:
        create_user_task.delay(email, user.id)
from allauth.account.signals import email_confirmed
from django.dispatch import receiver
from rest_framework.authtoken.models import Token
from django.conf import settings

from separator.users.tasks import create_user_task


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
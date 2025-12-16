from allauth.account.signals import email_confirmed, user_logged_in
from django.dispatch import receiver
from rest_framework.authtoken.models import Token

from separator.users.tasks import get_site

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
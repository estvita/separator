from django.contrib.sites.models import Site
from celery import shared_task
from .models import User

from separator.chatwoot.utils import create_chatwoot_user

@shared_task()
def get_users_count():
    """A pointless Celery task to demonstrate usage."""
    return User.objects.count()


@shared_task
def create_user_task(email, user_id):
    user = User.objects.get(id=user_id)
    create_chatwoot_user(email, user)


def get_site(request):
    host = request.get_host().split(':', 1)[0]
    site = Site.objects.filter(domain=host).first()
    if site:
        return site
    else:
        return None
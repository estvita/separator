from django.contrib.sites.models import Site
from celery import shared_task
from .models import User

@shared_task()
def get_users_count():
    """A pointless Celery task to demonstrate usage."""
    return User.objects.count()


def get_site(request):
    host = request.get_host().split(':', 1)[0]
    site = Site.objects.filter(domain=host).first()
    if site:
        return site
    else:
        return None
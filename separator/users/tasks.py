from django.contrib.sites.models import Site
from celery import shared_task
from celery.utils.log import get_task_logger
from .models import User

logger = get_task_logger(__name__)

@shared_task()
def get_users_count():
    """A pointless Celery task to demonstrate usage."""
    return User.objects.count()

@shared_task(queue='default', bind=True, max_retries=5, default_retry_delay=5)
def send_allauth_email_task(self, template_prefix, email, context):
    from separator.users.adapters import AccountAdapter
    
    render_context = context.copy()
    if 'user_id' in context:
        try:
            user = User.objects.get(pk=context['user_id'])
            render_context['user'] = user
        except User.DoesNotExist:
            logger.warning(f"User with id {context.get('user_id')} not found, skipping email.")
            return

    try:
        adapter = AccountAdapter()
        msg = adapter.render_mail(template_prefix, email, render_context)
        msg.send()
    except Exception as exc:
        logger.error(f"Failed to send email to {email}: {exc}")
        raise self.retry(exc=exc)


def get_site(request):
    host = request.get_host().split(':', 1)[0]
    site = Site.objects.filter(domain=host).first()
    if site:
        return site
    else:
        return None
from celery import shared_task
from django.conf import settings
from django.contrib.sites.models import Site
from thoth.waweb.models import Session
from thoth.chatwoot.models import Inbox

from thoth.chatwoot.utils import add_inbox


SITE_ID = settings.SITE_ID


@shared_task(bind=True)
def new_inbox(self, sessionid, number):

    try:
        site = Site.objects.get(id=SITE_ID)
        session = Session.objects.get(session=sessionid)

        inbox_data = {
            'name': number,
            'lock_to_single_conversation': True,
            'channel': {
                'type': 'api',
                'webhook_url': f'https://{site.domain}/api/waweb/{sessionid}/send/'
            }
        }
        resp = add_inbox(session.owner, inbox_data)
        if resp and "result" in resp:
            result = resp.get('result', {})
            inbox, created = Inbox.objects.update_or_create(
                owner=session.owner,
                id=result.get('inbox_id'),
                defaults={
                    'account': result.get('account'),
                }
            )
            session.inbox = inbox
            session.save()
            return {"status": "success", "inbox_id": inbox.id}
        else:
            raise Exception(f'No result in response: {resp}')
    except Exception as e:
        raise Exception(f'Failed to create/update Inbox: {str(e)}')
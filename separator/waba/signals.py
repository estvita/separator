from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Waba
from .tasks import waba_subscription


@receiver(post_save, sender=Waba)
def run_waba_subscription(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    transaction.on_commit(lambda: waba_subscription.delay(instance.id))

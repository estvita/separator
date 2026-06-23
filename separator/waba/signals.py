from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import App, Waba
from .tasks import fallback_subscription, waba_subscription


@receiver(post_save, sender=Waba)
def run_subscription(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    transaction.on_commit(lambda: waba_subscription.delay(instance.id))


@receiver(pre_save, sender=App)
def remember_app_fallback(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk:
        instance._old_fallback_app_id = None
        return
    instance._old_fallback_app_id = App.objects.filter(pk=instance.pk).values_list(
        "fallback_app_id",
        flat=True,
    ).first()


@receiver(post_save, sender=App)
def run_fallback_subscriptions(sender, instance, created, raw=False, **kwargs):
    if raw:
        return

    old_fallback_app_id = getattr(instance, "_old_fallback_app_id", None)
    new_fallback_app_id = instance.fallback_app_id
    if old_fallback_app_id == new_fallback_app_id:
        return

    waba_ids = list(Waba.objects.filter(app=instance).values_list("id", flat=True))
    if not waba_ids:
        return

    def schedule_fallback_subscriptions():
        if old_fallback_app_id:
            for waba_id in waba_ids:
                fallback_subscription.delay(waba_id, instance.id, old_fallback_app_id, subscribe=False)
        if new_fallback_app_id:
            for waba_id in waba_ids:
                fallback_subscription.delay(waba_id, instance.id, new_fallback_app_id, subscribe=True)

    transaction.on_commit(schedule_fallback_subscriptions)

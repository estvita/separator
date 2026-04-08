from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import OlxUser
from .utils import activate_task


@receiver(pre_save, sender=OlxUser)
def remember_previous_date_end(sender, instance, **kwargs):
    instance._previous_date_end = None
    if not instance.pk:
        return

    previous = sender.objects.filter(pk=instance.pk).only("date_end").first()
    if previous:
        instance._previous_date_end = previous.date_end


@receiver(post_save, sender=OlxUser)
def reactivate_task_on_date_end_change(sender, instance, created, **kwargs):
    if created:
        return

    previous_date_end = getattr(instance, "_previous_date_end", None)
    if previous_date_end == instance.date_end:
        return

    if instance.date_end and instance.date_end > timezone.now():
        activate_task(instance)

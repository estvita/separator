import json
import logging
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule
from django_celery_beat.models import PeriodicTask
from encrypted_fields.fields import EncryptedCharField

from separator.bitrix.models import Line

logger = logging.getLogger("django")


class OlxApp(models.Model):
    CLIENT_DOMAINS = [
        ("olx.kz", "olx.kz"),
        ("olx.bg", "olx.bg"),
        ("olx.ro", "olx.ro"),
        ("olx.ua", "olx.ua"),
        ("olx.pt", "olx.pt"),
        ("olx.pl", "olx.pl"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="olx_apps",
    )
    client_domain = models.CharField(max_length=10, choices=CLIENT_DOMAINS)
    client_id = models.CharField(max_length=255)
    client_secret = EncryptedCharField(max_length=255)
    authorization_link = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        editable=False,
    )

    def __str__(self):
        return f"{self.name} - {self.client_domain}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.authorization_link:
            self.authorization_link = f"https://www.{self.client_domain}/oauth/authorize/?client_id={self.client_id}&response_type=code&scope=read+write+v2&state={self.id}"
        super().save(*args, **kwargs)


class OlxUser(models.Model):
    olxapp = models.ForeignKey(
        OlxApp,
        on_delete=models.CASCADE,
        related_name="olx_users",
        null=True,
        blank=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="olx_users",
        blank=True,
        null=True,
    )
    line = models.ForeignKey(
        Line,
        on_delete=models.SET_NULL,
        related_name="olx_users",
        blank=True,
        null=True,
    )
    periodicity = models.PositiveIntegerField(
        default=10,
        help_text="Frequency of OLX server polling in minutes.",
    )
    date_end = models.DateTimeField(null=True, blank=True)
    olx_id = models.CharField(max_length=50, unique=True)
    email = models.EmailField(blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=255, blank=True, null=True)
    access_token = EncryptedCharField(
        max_length=4096,  # Увеличено до 4KB для JWT токенов (OLX requirement)
        blank=True,
        null=True,
        # editable=False,
    )
    refresh_token = EncryptedCharField(
        max_length=4096,  # Увеличено до 4KB для JWT токенов (OLX requirement)
        blank=True,
        null=True,
        # editable=False,
    )
    status = models.IntegerField(default=0, blank=True)
    attempts = models.IntegerField(default=0, blank=True)

    def __str__(self):
        return f"{self.name} ({self.olx_id})"

    def save(self, *args, **kwargs):
        if self.periodicity < 10:
            self.periodicity = 10
        super().save(*args, **kwargs)

        if self.line:
            self.add_shedule_task()

    def add_shedule_task(self):
        interval, created = IntervalSchedule.objects.get_or_create(
            every=self.periodicity,
            period=IntervalSchedule.MINUTES,
        )

        task_name = f"Pull threads {self.olx_id}"

        try:
            existing_task = PeriodicTask.objects.get(name=task_name)
            if existing_task.interval != interval:
                existing_task.interval = interval
                existing_task.save()

        except PeriodicTask.DoesNotExist:
            PeriodicTask.objects.create(
                name=task_name,
                task="separator.olx.tasks.get_threads",
                interval=interval,
                args=json.dumps([self.olx_id]),
                start_time=timezone.now(),
            )
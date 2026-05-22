import json
import logging
import uuid
from datetime import time
from datetime import timedelta

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


class OlxRegion(models.Model):
    client_domain = models.CharField(max_length=10, choices=OlxApp.CLIENT_DOMAINS)
    olx_id = models.PositiveIntegerField()
    name = models.CharField(max_length=255)

    class Meta:
        unique_together = ("client_domain", "olx_id")
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.client_domain})"


class OlxCity(models.Model):
    client_domain = models.CharField(max_length=10, choices=OlxApp.CLIENT_DOMAINS)
    olx_id = models.PositiveIntegerField()
    region = models.ForeignKey(
        OlxRegion,
        on_delete=models.SET_NULL,
        related_name="cities",
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=255)
    county = models.CharField(max_length=255, blank=True)
    municipality = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=6, blank=True, null=True)

    class Meta:
        unique_together = ("client_domain", "olx_id")
        ordering = ("name",)

    def __str__(self):
        parts = [self.name, self.municipality, self.county]
        return ", ".join(part for part in parts if part)


class OlxDistrict(models.Model):
    client_domain = models.CharField(max_length=10, choices=OlxApp.CLIENT_DOMAINS)
    olx_id = models.PositiveIntegerField()
    city = models.ForeignKey(OlxCity, on_delete=models.CASCADE, related_name="districts")
    name = models.CharField(max_length=255)
    latitude = models.DecimalField(max_digits=10, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=6, blank=True, null=True)

    class Meta:
        unique_together = ("client_domain", "olx_id")
        ordering = ("city__name", "name")

    def __str__(self):
        return f"{self.name}, {self.city.name}"


class OlxCategory(models.Model):
    client_domain = models.CharField(max_length=10, choices=OlxApp.CLIENT_DOMAINS)
    olx_id = models.PositiveIntegerField()
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="children",
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=255)
    photos_limit = models.PositiveIntegerField(default=0)
    is_leaf = models.BooleanField(default=False)

    class Meta:
        unique_together = ("client_domain", "olx_id")
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.olx_id})"


class OlxCategoryAttribute(models.Model):
    category = models.ForeignKey(
        OlxCategory,
        on_delete=models.CASCADE,
        related_name="attributes",
    )
    code = models.CharField(max_length=255)
    label = models.CharField(max_length=255)
    unit = models.CharField(max_length=50, blank=True, null=True)
    validation = models.JSONField(default=dict, blank=True)
    values = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = ("category", "code")
        ordering = ("label",)

    def __str__(self):
        return f"{self.category}: {self.label}"


class OlxAdvert(models.Model):
    olx_user = models.ForeignKey(
        OlxUser,
        on_delete=models.CASCADE,
        related_name="adverts",
    )
    advert_id = models.PositiveBigIntegerField()
    title = models.CharField(max_length=255)
    status = models.CharField(max_length=50, blank=True)
    url = models.URLField(max_length=500, blank=True, null=True)
    category = models.ForeignKey(
        OlxCategory,
        on_delete=models.SET_NULL,
        related_name="adverts",
        blank=True,
        null=True,
    )
    city = models.ForeignKey(
        OlxCity,
        on_delete=models.SET_NULL,
        related_name="adverts",
        blank=True,
        null=True,
    )
    district = models.ForeignKey(
        OlxDistrict,
        on_delete=models.SET_NULL,
        related_name="adverts",
        blank=True,
        null=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    pushup_interval_days = models.PositiveIntegerField(default=0)
    pushup_enabled = models.BooleanField(default=False)
    pushup_payment_method = models.CharField(max_length=20, default="account")
    pushup_time = models.TimeField(default=time(12, 0))
    last_pushup_at = models.DateTimeField(blank=True, null=True)
    next_pushup_at = models.DateTimeField(blank=True, null=True)
    last_pushup_error = models.TextField(blank=True)

    class Meta:
        unique_together = ("olx_user", "advert_id")
        ordering = ("-id",)

    def __str__(self):
        return f"{self.title} ({self.advert_id})"

    def calculate_next_pushup_at(self, base=None):
        if self.pushup_interval_days <= 0:
            return None
        base = base or timezone.now()
        pushup_time = self.pushup_time or time(12, 0)
        candidate = timezone.datetime.combine(base.date(), pushup_time)
        if timezone.is_naive(candidate):
            candidate = timezone.make_aware(candidate, timezone.get_current_timezone())
        if candidate <= base:
            candidate += timedelta(days=self.pushup_interval_days)
        return candidate


class OlxThread(models.Model):
    olx_user = models.ForeignKey(
        OlxUser,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    thread_id = models.PositiveBigIntegerField()
    last_message_id = models.PositiveBigIntegerField(default=0)
    total_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("olx_user", "thread_id")

    def __str__(self):
        return f"{self.olx_user.olx_id}: {self.thread_id}"

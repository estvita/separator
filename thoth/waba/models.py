import uuid

from django.conf import settings
from django.db import models
from django.contrib.sites.models import Site

from thoth.bitrix.models import AppInstance, Line
from thoth.chatwoot.models import Inbox

class App(models.Model):
    site = models.ForeignKey(
        Site, on_delete=models.CASCADE, related_name="waba_apps", blank=True, null=True
    )
    client_id = models.CharField(max_length=255, editable=True, unique=True)
    config_id = models.CharField(max_length=255, editable=True, null=True, blank=True)
    client_secret = models.CharField(max_length=255, editable=True)
    access_token = models.CharField(max_length=1000, null=True, blank=True)
    api_version = models.IntegerField(default=20)
    verify_token = models.CharField(
        max_length=100,
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )
    def __str__(self):
        return f"{self.client_id}"

class Waba(models.Model):
    waba_id = models.CharField(max_length=255, editable=True, unique=True)
    access_token = models.CharField(max_length=1000)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.waba_id}"


class Phone(models.Model):
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    phone_id = models.CharField(max_length=50, unique=True)
    inbox = models.ForeignKey(Inbox, on_delete=models.SET_NULL, null=True, blank=True)
    sms_service = models.BooleanField(default=True)
    waba = models.ForeignKey(Waba, on_delete=models.SET_NULL, related_name="phones", null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    app_instance = models.ForeignKey(AppInstance, on_delete=models.SET_NULL, related_name="phones", null=True, blank=True)
    line = models.ForeignKey(
        Line,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="phones",
    )
    date_end = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.phone} ({self.phone_id})"
    

class Template(models.Model):
    id = models.CharField(primary_key=True, max_length=255)
    waba = models.ForeignKey(Waba, on_delete=models.SET_NULL, related_name="templates", null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    lang = models.CharField(max_length=10)
    content = models.TextField()
    status = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} ({self.lang})"
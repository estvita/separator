import uuid

from django.conf import settings
from django.contrib.sites.models import Site
from django.db import models
from django.utils import timezone


def generate_uuid():
    return f"gulin_{uuid.uuid4()}"

class Connector(models.Model):
    TYPE_CHOICES = [
        ('olx', 'OLX'),
        ('waweb', 'WhatsApp Web'),
        ('waba', 'WhatsApp API'),
    ]
    code = models.CharField(max_length=255, default=generate_uuid, unique=True)
    service = models.CharField(max_length=255, choices=TYPE_CHOICES, blank=True, null=True)
    name = models.CharField(max_length=255, default="gulin.kz", unique=False)
    icon = models.FileField(upload_to='connector_icons/', blank=True, null=True)

    def __str__(self):
        return self.name
    

class App(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    site = models.ForeignKey(
        Site, on_delete=models.SET_NULL, related_name="apps", blank=True, null=True
    )
    name = models.CharField(max_length=255, blank=True, unique=False)
    page_url = models.CharField(max_length=255, blank=True, default="/")
    connectors = models.ManyToManyField(Connector, blank=True, related_name='apps')
    client_id = models.CharField(max_length=255, blank=True, unique=False)
    client_secret = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name


class Bitrix(models.Model):
    PROTOCOL_CHOICES = [
        ('http', 'HTTP'),
        ('https', 'HTTPS'),
    ]
    protocol = models.CharField(max_length=5, choices=PROTOCOL_CHOICES, default='https')
    domain = models.CharField(max_length=255)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    member_id = models.CharField(max_length=255, unique=True, blank=True, null=True)
    license_expired = models.BooleanField(default=False)

    def __str__(self):
        return self.domain

class User(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, 
                              on_delete=models.CASCADE, related_name='bitrix_owners',
                              blank=True, null=True)
    bitrix = models.ForeignKey(Bitrix, on_delete=models.CASCADE, related_name='users',
        blank=True,
        null=True)
    user_id = models.PositiveIntegerField()
    admin = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user_id} ({self.bitrix.domain})"


class AppInstance(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    app = models.ForeignKey(App, on_delete=models.SET_NULL, related_name="installations", blank=True, null=True)
    portal = models.ForeignKey(
        Bitrix, on_delete=models.CASCADE, related_name="installations", blank=True, null=True
    )
    auth_status = models.CharField(max_length=1)
    application_token = models.CharField(max_length=255, blank=True)
    storage_id = models.CharField(max_length=255, blank=True)
    status = models.IntegerField(default=0, blank=True)
    attempts = models.IntegerField(default=0, blank=True)

    def __str__(self):
        return f"{self.app.name} on {self.portal.domain}"


class Credential(models.Model):
    app_instance = models.ForeignKey(AppInstance, on_delete=models.CASCADE, related_name='credentials',
                               blank=True, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='credentials',
                             blank=True, null=True)
    access_token = models.CharField(max_length=255, blank=True)
    refresh_token = models.CharField(max_length=255, blank=True)


class AdminMessage(models.Model):
    app_instance = models.ForeignKey(AppInstance, on_delete=models.CASCADE, related_name='messages',
                                     null=True, blank=True)
    app_users = models.ManyToManyField(User, related_name='messages')
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.message


class Line(models.Model):
    line_id = models.CharField(max_length=50)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    app_instance = models.ForeignKey(AppInstance, on_delete=models.CASCADE, related_name="lines", null=True)
    connector = models.ForeignKey(Connector, on_delete=models.SET_NULL, related_name="lines", null=True)
    portal = models.ForeignKey(Bitrix, on_delete=models.CASCADE, related_name="lines", blank=True, null=True)
    def __str__(self):
        return f"Line {self.line_id} for AppInstance {self.app_instance}"


class VerificationCode(models.Model):
    portal = models.OneToOneField(Bitrix, on_delete=models.SET_NULL, blank=True, null=True)
    code = models.UUIDField(default=uuid.uuid4)
    expires_at = models.DateTimeField()

    def is_valid(self):
        return self.expires_at > timezone.now()

from django.db import models
from django.conf import settings
from separator.bitrix.models import AppInstance, Line
import uuid

class Server(models.Model):
    url = models.URLField(max_length=255, unique=True, verbose_name="Server URL", help_text="http://127.0.0.1:8080")
    api_key = models.CharField(max_length=255, verbose_name="API Key")
    max_connections = models.PositiveIntegerField(default=100)
    groups_ignore = models.BooleanField(default=True)
    always_online = models.BooleanField(default=False)
    read_messages = models.BooleanField(default=False)
    sync_history = models.BooleanField(default=True)

    def __str__(self):
        return self.url

class Session(models.Model):
    session = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    server = models.ForeignKey(Server, on_delete=models.SET_NULL, related_name="sessions", null=True, blank=True)
    apikey = models.CharField(max_length=255, blank=True, null=True)
    instanceId = models.CharField(max_length=255, blank=True, null=True)
    date_end = models.DateTimeField(null=True, blank=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    groups_ignore = models.BooleanField(default=True)
    sms_service = models.BooleanField(default=True)
    status = models.CharField(max_length=15, blank=True, null=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    app_instance = models.ForeignKey(
        AppInstance, on_delete=models.SET_NULL, related_name="wawebs", null=True, blank=True)
    line = models.ForeignKey(
        Line,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="wawebs",
    )

    def __str__(self):
        return f"Session: {self.session}, Phone: {self.phone or 'Not connected'}"

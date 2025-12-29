import uuid

from django.conf import settings
from django.db import models
from django.contrib.sites.models import Site
from encrypted_fields.fields import EncryptedCharField

from separator.bitrix.models import AppInstance, Line
from separator.freepbx.models import Server, Extension

class App(models.Model):
    events = models.BooleanField(default=False, help_text="Chek for save inbound events")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="waba_apps", default=1)
    client_id = models.CharField(max_length=255, editable=True, default='')
    config_id = models.CharField(max_length=255, editable=True, default='')
    client_secret = EncryptedCharField(max_length=500, editable=True, default='')
    access_token = EncryptedCharField(max_length=2000, default='',
                                    help_text="System admin user access_token")
    api_version = models.IntegerField(default=20)
    verify_token = models.CharField(
        max_length=100,
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )
    sip_server = models.ForeignKey(Server, on_delete=models.SET_NULL, blank=True, null=True)
    def __str__(self):
        return f"{self.client_id}"

class Waba(models.Model):
    app = models.ForeignKey(App, on_delete=models.SET_NULL, null=True, blank=True)
    waba_id = models.CharField(max_length=255, editable=True, unique=True)
    access_token = EncryptedCharField(max_length=2000)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.waba_id}"


class Phone(models.Model):
    TYPES_CHOICES = [
        ('cloud', 'cloud'),
        ('app', 'app'),
    ]
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    type = models.CharField(max_length=10, choices=TYPES_CHOICES, default="cloud")
    pin = models.CharField(max_length=6, default="000000")
    phone_id = models.CharField(max_length=50, unique=True)
    sms_service = models.BooleanField(default=True)
    waba = models.ForeignKey(Waba, on_delete=models.CASCADE, related_name="phones", null=True, blank=True)
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

    # Calling settings
    STATUS_CHOICES = [
        ('enabled', 'ENABLED'),
        ('disabled', 'DISABLED'),
    ]
    STRP_PROTOCOL = [
        ('DTLS', 'DTLS'),
        ('SDES', 'SDES'),
    ]
    CALL_DEST = [
        ('disabled', 'Disabled'),
        ('b24', 'Bitrix24'),
        ('ext', 'SIP Extension'),
        ('pbx', 'SIP Server'),
    ]
    call_dest = models.CharField(max_length=10, choices=CALL_DEST, default="disabled", blank=True)
    calling = models.CharField(max_length=10, choices=STATUS_CHOICES, default="disabled", blank=True)
    srtp_key_exchange_protocol = models.CharField(max_length=10, choices=STRP_PROTOCOL, default="SDES", blank=True)
    callback_permission_status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="enabled", blank=True)
    sip_status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="enabled", blank=True)
    sip_user_password = models.CharField(max_length=250, null=True, blank=True, help_text="Whatsapp Cloud SIP Password")
    sip_hostname = models.CharField(max_length=200, default="voip.gulin.kz", blank=True)
    sip_port = models.PositiveIntegerField(default=5061, blank=True)
    error = models.CharField(max_length=500, blank=True, null=True)
    sip_extensions = models.ForeignKey(Extension, on_delete=models.SET_NULL, null=True, blank=True)
    voximplant_id = models.PositiveIntegerField(blank=True, null=True)
    
    def save(self, *args, **kwargs):
        if self.phone:
            self.phone = '+' + ''.join(filter(str.isdigit, self.phone))
        super().save(*args, **kwargs)
    def __str__(self):
        return f"{self.phone} ({self.phone_id})"
    

class Template(models.Model):
    id = models.CharField(primary_key=True, max_length=255)
    waba = models.ForeignKey(Waba, on_delete=models.CASCADE, related_name="templates", null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    lang = models.CharField(max_length=10)
    content = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} ({self.lang})"


class Event(models.Model):
    waba = models.ForeignKey(Waba, on_delete=models.CASCADE, related_name="events", null=True, blank=True)
    date = models.DateTimeField(auto_now_add=True)
    content = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.id}"


class Error(models.Model):
    code = models.PositiveIntegerField()
    details = models.TextField(null=True, blank=True)
    message = models.TextField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.code}"
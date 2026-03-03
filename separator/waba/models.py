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
    subscribed = models.BooleanField(default=True, help_text="Subscribed to webhook events")

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
    availableInB24 = models.BooleanField(default=True)
    default = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.lang})"


class TemplateBroadcast(models.Model):
    STATUS_CHOICES = [
        ("pending", "pending"),
        ("sent", "sent"),
        ("delivered", "delivered"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ]

    template = models.ForeignKey(Template, on_delete=models.SET_NULL, null=True, blank=True, related_name="broadcasts")
    phone = models.ForeignKey(Phone, on_delete=models.CASCADE, related_name="broadcasts")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    text = models.TextField(null=True, blank=True)
    recipients_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=128, choices=STATUS_CHOICES, default="pending")
    scheduled_at = models.DateTimeField(null=True, blank=True)
    scheduled_task_id = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Broadcast"
        verbose_name_plural = "Broadcasts"


class TemplateBroadcastRecipient(models.Model):
    STATUS_CHOICES = [
        ("pending", "pending"),
        ("sent", "sent"),
        ("delivered", "delivered"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ]

    broadcast = models.ForeignKey(TemplateBroadcast, on_delete=models.CASCADE, related_name="recipients")
    recipient_phone = models.CharField(max_length=128)
    wamid = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=128, choices=STATUS_CHOICES, default="pending")
    error_json = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.recipient_phone} ({self.status})"

    class Meta:
        verbose_name = "Recipient"
        verbose_name_plural = "Recipients"


class TemplateComponent(models.Model):
    TYPE_CHOICES = [
        ("HEADER", "HEADER"),
        ("BODY", "BODY"),
        ("FOOTER", "FOOTER"),
        ("BUTTONS", "BUTTONS"),
    ]
    FORMAT_CHOICES = [
        ("TEXT", "TEXT"),
        ("IMAGE", "IMAGE"),
        ("VIDEO", "VIDEO"),
        ("GIF", "GIF"),
        ("DOCUMENT", "DOCUMENT"),
        ("LOCATION", "LOCATION"),
    ]

    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="components")
    type = models.CharField(max_length=128, choices=TYPE_CHOICES)
    format = models.CharField(max_length=128, choices=FORMAT_CHOICES, null=True, blank=True)
    text = models.TextField(null=True, blank=True)
    index = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f"{self.template_id}:{self.type}#{self.index}"

    class Meta:
        verbose_name = "Component"
        verbose_name_plural = "Components"


class TemplateComponentButton(models.Model):
    TYPE_CHOICES = [
        ("QUICK_REPLY", "QUICK_REPLY"),
        ("URL", "URL"),
        ("PHONE_NUMBER", "PHONE_NUMBER"),
        ("VOICE_CALL", "VOICE_CALL"),
        ("COPY_CODE", "COPY_CODE"),
        ("MPM", "MPM"),
        ("SPM", "SPM"),
        ("OTP", "OTP"),
    ]

    component = models.ForeignKey(TemplateComponent, on_delete=models.CASCADE, related_name="buttons")
    type = models.CharField(max_length=128, choices=TYPE_CHOICES)
    text = models.CharField(max_length=255, null=True, blank=True)
    url = models.TextField(null=True, blank=True)
    phone_number = models.CharField(max_length=32, null=True, blank=True)
    example = models.TextField(null=True, blank=True)
    index = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f"{self.component_id}:{self.type}#{self.index}"

    class Meta:
        verbose_name = "Button"
        verbose_name_plural = "Buttons"


class TemplateComponentNamedParam(models.Model):
    component = models.ForeignKey(TemplateComponent, on_delete=models.CASCADE, related_name="named_params")
    button = models.ForeignKey(TemplateComponentButton, on_delete=models.CASCADE, null=True, blank=True, related_name="named_params")
    name = models.CharField(max_length=255)
    example = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["component", "button", "name"], name="uniq_component_named_param"),
        ]

    def __str__(self):
        return f"{self.component_id}:{self.name}"

    class Meta:
        verbose_name = "NamedParam"
        verbose_name_plural = "NamedParams"


class TemplateComponentPositionalParam(models.Model):
    component = models.ForeignKey(TemplateComponent, on_delete=models.CASCADE, related_name="positional_params")
    button = models.ForeignKey(TemplateComponentButton, on_delete=models.CASCADE, null=True, blank=True, related_name="positional_params")
    position = models.PositiveSmallIntegerField()
    example = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["component", "button", "position"], name="uniq_component_positional_param"),
        ]

    def __str__(self):
        return f"{self.component_id}:{self.position}"

    class Meta:
        verbose_name = "PosParam"
        verbose_name_plural = "PosParams"


class Event(models.Model):
    waba = models.ForeignKey(Waba, on_delete=models.CASCADE, related_name="events", null=True, blank=True)
    date = models.DateTimeField(auto_now_add=True)
    content = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.id}"


class Error(models.Model):
    code = models.PositiveIntegerField()
    original = models.BooleanField(default=True)
    fallback = models.BooleanField(default=False)
    details = models.TextField(null=True, blank=True)
    message = models.TextField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.code}"

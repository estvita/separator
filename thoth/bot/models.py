import uuid
import secrets
from django.db import models
from django.conf import settings
from thoth.chatwoot.models import AgentBot
from thoth.bitrix.models import AppInstance

class Provider(models.Model):
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name

class Model(models.Model):
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=25, default="text")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="models")
    max_completion_tokens = models.PositiveIntegerField(default=4096)

    def __str__(self):
        return self.name

class ApiKey(models.Model):
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="api_keys")
    key = models.CharField(max_length=1500)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.provider.name} (id: {self.id}) - {self.owner}"
    

class Feature(models.Model):
    PRIVACY = [
        ("private", "private"),
        ("public", "public"),
    ]

    TYPES = [
        ("instruction", "instruction"),
        ("function", "function"),
    ]

    ENGINE = [
        ("text", "text"),
        ("voice", "voice"),
    ]

    name = models.CharField(max_length=255)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    description_human = models.TextField()
    description_openai = models.JSONField()
    type = models.CharField(max_length=50, choices=TYPES)
    engine = models.CharField(max_length=50, choices=ENGINE, default="text")
    privacy = models.CharField(max_length=50, choices=PRIVACY)

    def __str__(self):
        return self.name

class Bot(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    agent_bot = models.ForeignKey(AgentBot, on_delete=models.SET_NULL, related_name="bots", null=True, blank=True)
    name = models.CharField(max_length=255)
    expiration_date = models.DateTimeField(null=True, blank=True)
    follow_up = models.CharField(max_length=255, default="follow_up")
    bitrix = models.ForeignKey(AppInstance, on_delete=models.SET_NULL, related_name="bots", null=True, blank=True)
    model = models.ForeignKey(Model, on_delete=models.SET_NULL, related_name="bots", null=True, blank=True)
    features = models.ManyToManyField(Feature, related_name="bots", blank=True)
    assistant_id = models.CharField(max_length=255, null=True, blank=True)
    vector_store = models.CharField(max_length=255, null=True, blank=True)
    token = models.ForeignKey(ApiKey, on_delete=models.SET_NULL, related_name="bots", null=True, blank=True)
    system_message = models.TextField()
    speech_to_text = models.BooleanField(default=True)
    stt_model = models.CharField(max_length=255, default="whisper-1")
    memory_days = models.PositiveIntegerField(default=2)
    memory_count = models.PositiveIntegerField(default=15)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=1.0)
    max_completion_tokens = models.PositiveIntegerField(default=4096)
    top_p = models.DecimalField(max_digits=3, decimal_places=2, default=0.9)
    frequency_penalty = models.DecimalField(max_digits=3, decimal_places=2, default=0.0)
    presence_penalty = models.DecimalField(max_digits=3, decimal_places=2, default=0.0)

    def __str__(self):
        return self.name

def generate_uuid():
    return uuid.uuid4().hex


class Vocal(models.Model):
    vocal = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.vocal


class Voice(models.Model):
    id = models.CharField(primary_key=True, max_length=32, default=generate_uuid)
    name = models.CharField(max_length=255)
    expiration_date = models.DateTimeField(null=True, blank=True)
    password = models.CharField(max_length=255, null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    token = models.ForeignKey('ApiKey', on_delete=models.SET_NULL, related_name="voices", null=True, blank=True)
    model = models.ForeignKey('Model', on_delete=models.SET_NULL, related_name="voices", null=True, blank=True)
    vocal = models.ForeignKey('Vocal', on_delete=models.SET_NULL, related_name="voices", null=True, blank=True)
    instruction = models.TextField()
    welcome_msg = models.TextField(null=True, blank=True)
    transfer_uri = models.CharField(max_length=255, null=True, blank=True)
    features = models.ManyToManyField(Feature, related_name="voices", blank=True)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=1.0)
    max_tokens = models.PositiveIntegerField(default=4096)

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        
        if not self.password:
            self.password = secrets.token_urlsafe(25)

        super().save(*args, **kwargs)
        
        if is_new:
            from thoth.bot.tasks import manage_sip_user
            manage_sip_user.delay("add", self.id, self.password)

    def delete(self, *args, **kwargs):
        from thoth.bot.tasks import manage_sip_user
        manage_sip_user.delay("delete", self.id)
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.name}: {self.id}"
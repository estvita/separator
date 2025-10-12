from django.db import models
from separator.chatwoot.models import AgentBot

# Create your models here.
from django.conf import settings


class Dify(models.Model):
    TYPE_CHOICES = [
        ('chatflow', 'Chatflow'),
        ('workflow', 'Workflow'),
    ]
    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default="chatflow")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dify')
    base_url = models.URLField(default='https://api.dify.ai/v1')
    api_key = models.CharField(max_length=255)
    agent_bot = models.ForeignKey(AgentBot, on_delete=models.SET_NULL, related_name="dify", null=True, blank=True)
    expiration_date = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.id} - {self.owner}"
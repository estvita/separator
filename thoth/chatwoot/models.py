from django.db import models
from django.conf import settings


class Chatwoot(models.Model):
    url = models.URLField(default='https://chat.thoth.kz/')
    platform_key = models.CharField(max_length=255)
    default_role = models.CharField(max_length=255, default="agent")
    api_version = models.CharField(
        max_length=10,
        default='v1',
    )

    def __str__(self):
        return self.url


class Account(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    id = models.CharField(primary_key=True, max_length=255)

    def __str__(self):
        return self.id


class User(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    id = models.CharField(primary_key=True, max_length=255)
    account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True)
    access_token = models.CharField(max_length=500)

    def __str__(self):
        return self.id


class PhoneNumber(models.Model):
    phone = models.CharField(max_length=20, unique=True)

    def __str__(self):
        return self.phone


class Inbox(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    id = models.CharField(primary_key=True, max_length=255)
    account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.id


class AgentBot(models.Model):
    id = models.AutoField(primary_key=True)
    token = models.CharField(max_length=500)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="agent_bots")

    def __str__(self):
        return f"AgentBot {self.id}"


class Feature(models.Model):
    server = models.ForeignKey(Chatwoot, on_delete=models.CASCADE, related_name="features")
    name = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} - {'Enabled' if self.is_enabled else 'Disabled'}"


class Limit(models.Model):
    server = models.ForeignKey(Chatwoot, on_delete=models.CASCADE, related_name="limits")
    name = models.CharField(max_length=255)
    value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.name}: {self.value}"

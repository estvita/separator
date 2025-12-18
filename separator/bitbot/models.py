from django.db import models
from django.conf import settings
from django.contrib.sites.models import Site
from separator.bitrix.models import AppInstance

# Create your models here.
class Provider(models.Model):
    TYPE_CHOICES = (
        ('typebot', 'Typebot'),
        ('dify_chatflow', 'Dify Chatflow'),
        ('dify_workflow', 'Dify Workflow'),
    )
    name = models.CharField(max_length=255)
    site = models.ForeignKey(Site, on_delete=models.SET_NULL, related_name="chatbotproviders", null=True)
    type = models.CharField(max_length=255, choices=TYPE_CHOICES)
    url = models.CharField(max_length=1020, blank=True, null=True)
    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"


class Connector(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, related_name="connectors", null=True)
    url = models.URLField(max_length=1020, blank=True, null=True)
    key = models.CharField(max_length=1020)

    def __str__(self):
        return f"{str(self.id)} - {self.provider}"

# https://apidocs.bitrix24.com/api-reference/chat-bots/imbot-register.html
class ChatBot(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="chatbots")
    date_end = models.DateTimeField(null=True, blank=True)
    app_instance = models.ForeignKey(AppInstance, on_delete=models.SET_NULL, related_name="chatbots", null=True)
    name = models.CharField(max_length=255, default="BitBot")
    bot_id = models.PositiveIntegerField(default=0)
    bot_type = models.CharField(max_length=1, default="O", help_text="B, O or S")

    def __str__(self):
        return f"{self.name} - {self.id}"


# https://apidocs.bitrix24.com/api-reference/chat-bots/commands/imbot-command-register.html

class Command(models.Model):
    bot = models.ForeignKey(ChatBot, on_delete=models.CASCADE, related_name="commands")
    command_id = models.PositiveIntegerField(default=0)
    command = models.CharField(max_length=255, default="help")
    common = models.CharField(max_length=1, default="Y", help_text="Y or N")
    hidden = models.CharField(max_length=1, default="N", help_text="Y or N")
    extranet = models.CharField(max_length=1, default="N", help_text="Y or N")

    def __str__(self):
        return f"{self.command} - {self.id}"


class CommandLang(models.Model):
    command = models.ForeignKey(Command, on_delete=models.CASCADE, related_name="langs")
    language = models.CharField(max_length=10, default="ru", help_text="en, ru, de..")
    title = models.CharField(max_length=500)
    params = models.CharField(max_length=500, blank=True, null=True)

    class Meta:
        unique_together = ('command', 'language')

    def __str__(self):
        return f"{self.title} - {self.id}"
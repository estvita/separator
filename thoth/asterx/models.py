import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from thoth.bitrix.models import AppInstance

class Settings(models.Model):
    app_instance = models.ForeignKey(
        AppInstance, on_delete=models.CASCADE, related_name="asterx_settings", null=True, blank=True
    )
    show_card = models.IntegerField(
        default=2,
        help_text=_("0 - not show, 1 - on call, 2 - on answer")
    )
    crm_create = models.BooleanField(
        default=True,
        help_text=_("Create Deal on B24")
        )
    vm_send = models.BooleanField(
        default=True,
        help_text=_("Send VoiceMail to B24")
    )
    smart_route = models.BooleanField(
        default=False,
        help_text=_("Find a manager in Bitrix and connect with him")
    )

    def __str__(self):
        return str(self.app_instance)


class Server(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, default=_("My PBX"))
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    date_end = models.DateTimeField(null=True, blank=True)
    setup_complete = models.BooleanField(default=False)
    settings = models.ForeignKey(
        Settings, on_delete=models.SET_NULL, related_name="servers", null=True, blank=True)
    version = models.CharField(max_length=255, null=True, blank=True)
    system = models.CharField(max_length=255, null=True, blank=True)
    entity_id = models.CharField(max_length=255, null=True, blank=True)
    pbx_uuid = models.UUIDField(null=True, blank=True)
    
    def __str__(self):
        return str(self.id)
    

class Context(models.Model):
    TYPE_CHOICES = [
        ('exclude', _('Exclude')),
        ('external', _('External')),
        ('internal', _('Internal')),
    ]
    server = models.ForeignKey(Server, on_delete=models.CASCADE)
    context = models.CharField(max_length=255, default="from-internal")
    endpoint = models.CharField(max_length=255, default="100")
    type = models.CharField(
        max_length=10,
        choices=TYPE_CHOICES,
        default='exclude'
    )
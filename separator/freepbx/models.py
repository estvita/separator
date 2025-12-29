from django.db import models
from django.conf import settings
from encrypted_fields.fields import EncryptedCharField

class Server(models.Model):
    domain = models.CharField(max_length=255, default="voip.gulin.kz")
    sip_port = models.PositiveIntegerField(default=5061)
    ext_digits = models.PositiveIntegerField(default=5)
    client_id = EncryptedCharField(max_length=500, unique=True)
    client_secret = EncryptedCharField(max_length=500)
    gql_scopes = models.CharField(max_length=500, verbose_name="Allowed Scopes", default="gql:framework gql:core")
    
    def __str__(self):
        return self.domain


class Extension(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="sip_extensions", null=True)
    server = models.ForeignKey(Server, on_delete=models.SET_NULL, related_name="extensions", null=True)
    number = models.PositiveIntegerField(unique=True)
    password = EncryptedCharField(max_length=255)
    date_end = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return str(self.number)
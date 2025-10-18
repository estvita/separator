from django.db import models
from django.conf import settings
from django.contrib.sites.models import Site

class Service(models.Model):
    name = models.CharField(max_length=255, unique=True)
    code = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name

class Tariff(models.Model):
    PERIOD_CHOICES = [
        ("day", "День"),
        ("month", "Месяц"),
        ("year", "Год"),
    ]

    CURRENCY_CHOICES = [
        ("USD", "Доллар США"),
        ("EUR", "Евро"),
        ("RUB", "Рубль"),
        ("KZT", "Тенге"),
    ]

    site = models.ForeignKey(Site, on_delete=models.SET_NULL, related_name="tariffs", blank=True, null=True)
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="tariffs", blank=True, null=True)
    is_trial = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    currency = models.CharField(
        max_length=10, 
        choices=CURRENCY_CHOICES,
        blank=True, null=True
    )
    duration = models.PositiveIntegerField(blank=True, null=True)
    period = models.CharField(
        max_length=10,
        choices=PERIOD_CHOICES,
        default="day",
        blank=True, null=True
    )
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.service.name


class Trial(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trials")
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="trials")

    def __str__(self):
        return str(self.id)
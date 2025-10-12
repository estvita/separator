from django.db import models
from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db.models import CharField
from django.db.models import EmailField
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from phonenumber_field.modelfields import PhoneNumberField

from .managers import UserManager


class User(AbstractUser):
    """
    Default custom user model for separator.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, null=True, max_length=255)
    first_name = CharField(_("First Name"), blank=True, null=True, max_length=150)
    last_name = CharField(_("Last Name"), blank=True, null=True, max_length=150)
    email = EmailField(_("email address"), unique=True)
    phone_number = PhoneNumberField(blank=True, null=True)
    integrator = models.BooleanField(default=False)
    username = None  # type: ignore[assignment]

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects: ClassVar[UserManager] = UserManager()

    def get_absolute_url(self) -> str:
        """Get URL for user's detail view.

        Returns:
            str: URL for user detail.

        """
        return reverse("users:detail", kwargs={"pk": self.id})
    

class Message(models.Model):
    code = models.CharField(max_length=255)
    message = models.TextField()

    def __str__(self):
        return self.code
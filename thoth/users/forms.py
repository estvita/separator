from allauth.account.forms import SignupForm
# from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from django.contrib.auth import forms as admin_forms
from django.forms import EmailField
from django import forms
from django.utils.translation import gettext_lazy as _

from phonenumber_field.formfields import PhoneNumberField

from .models import User


class UserAdminChangeForm(admin_forms.UserChangeForm):
    class Meta(admin_forms.UserChangeForm.Meta):  # type: ignore[name-defined]
        model = User
        field_classes = {"email": EmailField}


class UserAdminCreationForm(admin_forms.UserCreationForm):
    """
    Form for User Creation in the Admin Area.
    To change user signup, see UserSignupForm and UserSocialSignupForm.
    """

    class Meta(admin_forms.UserCreationForm.Meta):  # type: ignore[name-defined]
        model = User
        fields = ("email",)
        field_classes = {"email": EmailField}
        error_messages = {
            "email": {"unique": _("This email has already been taken.")},
        }


class UserSignupForm(SignupForm):
    phone_number = PhoneNumberField(
        label=_("Phone number"),
        required=True,
        region="KZ",
        widget=forms.TextInput(
            attrs={
                "placeholder": "+77071234567",
            }
        )
    )

    def save(self, request):
        user = super().save(request)
        user.phone_number = self.cleaned_data.get("phone_number")
        user.save()
        return user


# class UserSocialSignupForm(SocialSignupForm):
#     """
#     Renders the form when user has signed up using social accounts.
#     Default fields will be added automatically.
#     See UserSignupForm otherwise.
#     """

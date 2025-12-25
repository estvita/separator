from django import forms
from django.utils.translation import gettext_lazy as _

class SendMessageForm(forms.Form):
    recipients = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": _("Enter recipient numbers, one per line")}),
        label=None
    )
    message = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": _("Enter message text")}),
        label=None
    )

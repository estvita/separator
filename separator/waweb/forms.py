from django import forms
from django.utils.translation import gettext_lazy as _

class SendMessageForm(forms.Form):
    recipients = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": _("Enter recipient numbers, one per line")}),
        label=None
    )
    message = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": _("Enter message text")}),
        label=None,
        required=False
    )
    file = forms.FileField(
        label=_("Attachment"),
        required=False
    )

    def clean(self):
        cleaned_data = super().clean()
        message = cleaned_data.get("message")
        file = cleaned_data.get("file")

        if not message and not file:
            raise forms.ValidationError(_("You must provide either a message or a file."))
        
        return cleaned_data

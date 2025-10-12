# forms.py
from django import forms
from .models import Phone

class WhatsAppMessageForm(forms.Form):
    phone_sender = forms.ModelChoiceField(
        queryset=Phone.objects.all(),
        label="Select Phone",
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    template = forms.CharField(
        label="Template",
        initial="hello_separator",
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    locale = forms.CharField(
        label="Localization",
        max_length=10,
        initial='en_US',
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    recipient_phones = forms.CharField(
        label="Recipient Phones",
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
        help_text="Enter one phone per line"
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user:
            self.fields['phone_sender'].queryset = Phone.objects.filter(owner=user)

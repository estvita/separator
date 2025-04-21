from django import forms

class SendMessageForm(forms.Form):
    recipients = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": "Введите номера получателей, по одному в строке"}),
        label=None
    )
    message = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": "Введите текст сообщения"}),
        label=None
    )

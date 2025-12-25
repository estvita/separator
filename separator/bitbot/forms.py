from django import forms
from django.utils.translation import gettext_lazy as _
from .models import ChatBot, Connector, AppInstance, Command, CommandLang

class ConnectorForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['url'].required = False
    class Meta:
        model = Connector
        fields = ['provider', 'key', 'url']
        widgets = {
            'provider': forms.Select(attrs={'class': 'form-control'}),
            'key': forms.PasswordInput(attrs={'class': 'form-control'}),
            'url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': _('Optional')}),
        }

class ChatBotForm(forms.ModelForm):
    app_instance = forms.ModelChoiceField(
        queryset=AppInstance.objects.none(), required=True, label=_("Bitrix24"),
        widget=forms.Select(attrs={"class": "form-control"})
    )
    class Meta:
        model = ChatBot
        fields = ['name', 'bot_type', 'app_instance', 'connector']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'bot_type': forms.Select(choices=[("O", "Open Lines"), ("B", "Chat Bot"), ("S", "Supervisor")],
                                     attrs={'class': 'form-control'}),
            'connector': forms.Select(attrs={'class': 'form-control'}),
        }
    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user")
        super().__init__(*args, **kwargs)
        self.fields['app_instance'].queryset = AppInstance.objects.filter(owner=user, app__bitbot=True)
        self.fields['connector'].queryset = Connector.objects.filter(owner=user)

class CommandCreateForm(forms.ModelForm):
    class Meta:
        model = Command
        fields = ['command', 'common', 'hidden', 'extranet']
        widgets = {
            'command': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('help')}),
            'common': forms.Select(choices=[('Y', 'Y'), ('N', 'N')], attrs={'class': 'form-control'}),
            'hidden': forms.Select(choices=[('N', 'N'), ('Y', 'Y')], attrs={'class': 'form-control'}),
            'extranet': forms.Select(choices=[('N', 'N'), ('Y', 'Y')], attrs={'class': 'form-control'}),
        }

class CommandLangForm(forms.ModelForm):
    class Meta:
        model = CommandLang
        fields = ['language', 'title', 'params']
        widgets = {
            'language': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ru'}),
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Title'}),
            'params': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'optional'}),
        }
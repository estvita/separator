from django import forms
from django.db import models
from .models import Bot, Model, ApiKey, Feature, Voice, Vocal

class ApiKeyForm(forms.ModelForm):
    class Meta:
        model = ApiKey
        fields = ['provider', 'key']
        widgets = {
            'key': forms.TextInput(attrs={'placeholder': 'Введите API ключ'}),
        }

class BotForm(forms.ModelForm):
    functions = forms.ModelMultipleChoiceField(
        queryset=Feature.objects.none(),
        widget=forms.SelectMultiple(attrs={'size': '5'}),
        required=False,
    )

    instructions = forms.ModelMultipleChoiceField(
        queryset=Feature.objects.none(),
        widget=forms.SelectMultiple(attrs={'size': '5'}),
        required=False,
    )

    class Meta:
        model = Bot
        fields = ['name', 'model', 'system_message', 'speech_to_text',
                  'temperature', 'max_completion_tokens', 'top_p', 'frequency_penalty', 'presence_penalty']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Название'}),
            'speech_to_text': forms.CheckboxInput(),
            'system_message': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Опишите боту его задачу.'}),
            'temperature': forms.NumberInput(attrs={'step': '0.01', 'max': '2'}),
            'max_completion_tokens': forms.NumberInput(attrs={'step': '1'}),
            'top_p': forms.NumberInput(attrs={'step': '0.01', 'max': '1'}),
            'frequency_penalty': forms.NumberInput(attrs={'step': '0.01', 'max': '2'}),
            'presence_penalty': forms.NumberInput(attrs={'step': '0.01', 'max': '2'}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['name'].initial = 'Thoth Bot'
        self.fields['model'].queryset = Model.objects.filter(type="text")
        self.fields['model'].required = True
        self.fields['model'].label_from_instance = lambda obj: f"{obj.name} (max tokens: {obj.max_completion_tokens})"

        # Динамическая фильтрация функций и инструкций
        if user:
            self.fields['functions'].queryset = Feature.objects.filter(
                type="function",
                engine="text"
            ).filter(
                models.Q(privacy="public") | models.Q(privacy="private", owner=user)
            )
            self.fields['instructions'].queryset = Feature.objects.filter(
                type="instruction"
            ).filter(
                models.Q(privacy="public") | models.Q(privacy="private", owner=user)
            )

        # Предзаполнение выбранных функций для существующего бота
        if self.instance and self.instance.pk:
            self.fields['functions'].initial = self.instance.features.filter(type="function")
            self.fields['instructions'].initial = self.instance.features.filter(type="instruction")


class VoiceForm(forms.ModelForm):
    functions = forms.ModelMultipleChoiceField(
        queryset=Feature.objects.none(),
        widget=forms.SelectMultiple(attrs={'size': '5'}),
        required=False,
    )
    
    class Meta:
        model = Voice
        fields = ["name", "model", "vocal", "instruction", "welcome_msg", "transfer_uri", "temperature", "max_tokens"]
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Название'}),
            'instruction': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Опишите боту его задачу'}),
            'welcome_msg': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Приветствие'}),
            'transfer_uri': forms.TextInput(attrs={'placeholder': 'SIP URI для трансфера'}),
            'temperature': forms.NumberInput(attrs={'step': '0.01', 'max': '2'}),
            'max_tokens': forms.NumberInput(attrs={'step': '1'}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['name'].initial = 'Thoth Voice'
        self.fields['vocal'].queryset = Vocal.objects.all()
        self.fields['vocal'].required = True
        self.fields['vocal'].empty_label = "Голос"
        self.fields['model'].queryset = Model.objects.filter(type="voice")
        self.fields['model'].empty_label = "Model"
        self.fields['model'].required = True
        self.fields['model'].label_from_instance = lambda obj: f"{obj.name} (max tokens: {obj.max_completion_tokens})"

        if user:
            self.fields['functions'].queryset = Feature.objects.filter(
                type="function",
                engine="voice"
            ).filter(
                models.Q(privacy="public") | models.Q(privacy="private", owner=user)
            )

        if self.instance and self.instance.pk:
            self.fields['functions'].initial = self.instance.features.filter(type="function")
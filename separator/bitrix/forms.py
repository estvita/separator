from django import forms


class BitrixPortalForm(forms.Form):
    portal_address = forms.CharField(max_length=255, 
                                     widget=forms.TextInput(attrs={'placeholder': 'crm.bitrix24.com'}))


class VerificationCodeForm(forms.Form):
    confirmation_code = forms.CharField(max_length=255, 
                                        widget=forms.TextInput(attrs={'placeholder': 'Код подтверждения'}))

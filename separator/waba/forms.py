import json

from django import forms
from django.utils.translation import gettext_lazy as _
from .models import Interactive, PartnerApp, Phone


class PartnerAppForm(forms.ModelForm):
    class Meta:
        model = PartnerApp
        fields = ("name", "webhook_url", "redirect_url", "active")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "webhook_url": forms.URLInput(attrs={"class": "form-control"}),
            "redirect_url": forms.URLInput(attrs={"class": "form-control"}),
            "active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

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


class InteractiveForm(forms.ModelForm):
    HEADER_CHOICES = [
        ("", _("No header")),
        ("text", _("Text")),
        ("image", _("Image URL")),
        ("video", _("Video URL")),
        ("document", _("Document URL")),
    ]

    header_type = forms.ChoiceField(
        choices=HEADER_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    header_value = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    body = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )
    footer = forms.CharField(
        required=False,
        max_length=60,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    variables_json = forms.CharField(required=False, widget=forms.HiddenInput())
    buttons_json = forms.CharField(required=False, widget=forms.HiddenInput())
    sections_json = forms.CharField(required=False, widget=forms.HiddenInput())
    list_button = forms.CharField(
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    cta_display_text = forms.CharField(
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    cta_url = forms.URLField(
        required=False,
        widget=forms.URLInput(attrs={"class": "form-control"}),
    )
    voice_call_display_text = forms.CharField(
        required=False,
        initial=_("Call on WhatsApp"),
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    voice_call_ttl_minutes = forms.IntegerField(
        required=False,
        initial=10080,
        min_value=1,
        max_value=43200,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 43200}),
    )
    voice_call_payload = forms.CharField(
        required=False,
        max_length=512,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    class Meta:
        model = Interactive
        fields = ("portal", "name", "type")
        widgets = {
            "portal": forms.Select(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "type": forms.Select(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        portals = kwargs.pop("portals", None)
        super().__init__(*args, **kwargs)
        if portals is not None:
            self.fields["portal"].queryset = portals
            self.fields["portal"].required = True
            if not self.instance.pk and portals.count() == 1:
                self.fields["portal"].initial = portals.first()
        payload = self.instance.payload if self.instance and self.instance.pk else {}
        header = payload.get("header") or {}
        if payload:
            self.fields["header_type"].initial = header.get("type", "")
            self.fields["header_value"].initial = header.get("value", "")
            self.fields["body"].initial = payload.get("body", "")
            self.fields["footer"].initial = payload.get("footer", "")
            self.fields["variables_json"].initial = json.dumps(payload.get("variables", []), ensure_ascii=False)
            self.fields["buttons_json"].initial = json.dumps(payload.get("buttons", []), ensure_ascii=False)
            self.fields["sections_json"].initial = json.dumps(payload.get("sections", []), ensure_ascii=False)
            self.fields["list_button"].initial = payload.get("button", "")
            self.fields["cta_display_text"].initial = payload.get("display_text", "")
            self.fields["cta_url"].initial = payload.get("url", "")
            self.fields["voice_call_display_text"].initial = payload.get("display_text", _("Call on WhatsApp"))
            self.fields["voice_call_ttl_minutes"].initial = payload.get("ttl_minutes", 10080)
            self.fields["voice_call_payload"].initial = payload.get("call_payload", "")

    def _load_json_list(self, field):
        raw = self.cleaned_data.get(field) or "[]"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise forms.ValidationError("Invalid JSON")
        if not isinstance(value, list):
            raise forms.ValidationError("Invalid list")
        return value

    def clean(self):
        cleaned = super().clean()
        message_type = cleaned.get("type")
        body = (cleaned.get("body") or "").strip()
        footer = (cleaned.get("footer") or "").strip()
        header_type = cleaned.get("header_type") or ""
        header_value = (cleaned.get("header_value") or "").strip()

        if message_type in {"button", "cta_url", "voice_call", "call_permission_request"} and len(body) > 1024:
            self.add_error("body", "Maximum 1024 characters.")
        if message_type == "list" and len(body) > 4096:
            self.add_error("body", "Maximum 4096 characters.")
        if footer and len(footer) > 60:
            self.add_error("footer", "Maximum 60 characters.")
        if header_type and not header_value and message_type not in {"voice_call", "call_permission_request"}:
            self.add_error("header_value", "Header value is required.")
        if message_type == "list" and header_type and header_type != "text":
            self.add_error("header_type", "List messages support text header only.")
        if header_type == "text" and len(header_value) > 60 and message_type not in {"voice_call", "call_permission_request"}:
            self.add_error("header_value", "Maximum 60 characters.")

        payload = {"body": body}
        variables = self._load_json_list("variables_json")
        clean_variables = []
        seen_variables = set()
        for variable in variables:
            name = str(variable.get("name", "")).strip()
            example = str(variable.get("example", "")).strip()
            if not name and not example:
                continue
            if not name:
                self.add_error("variables_json", "Variable name is required.")
                continue
            if name in seen_variables:
                self.add_error("variables_json", "Variable names must be unique.")
            seen_variables.add(name)
            clean_variables.append({"name": name, "example": example})
        if clean_variables:
            payload["variables"] = clean_variables
        if header_type and message_type not in {"voice_call", "call_permission_request"}:
            payload["header"] = {"type": header_type, "value": header_value}
        if footer and message_type not in {"voice_call", "call_permission_request"}:
            payload["footer"] = footer

        if message_type == "button":
            buttons = self._load_json_list("buttons_json")
            if not buttons or len(buttons) > 3:
                self.add_error("buttons_json", "Add from 1 to 3 buttons.")
            seen_ids = set()
            clean_buttons = []
            for button in buttons:
                button_id = str(button.get("id", "")).strip()
                title = str(button.get("title", "")).strip()
                if not button_id or not title:
                    self.add_error("buttons_json", "Button ID and title are required.")
                    continue
                if button_id in seen_ids:
                    self.add_error("buttons_json", "Button IDs must be unique.")
                seen_ids.add(button_id)
                if len(button_id) > 256:
                    self.add_error("buttons_json", "Button ID maximum is 256 characters.")
                if len(title) > 20:
                    self.add_error("buttons_json", "Button title maximum is 20 characters.")
                clean_buttons.append({"id": button_id, "title": title})
            payload["buttons"] = clean_buttons

        elif message_type == "list":
            button_text = (cleaned.get("list_button") or "").strip()
            if not button_text:
                self.add_error("list_button", "Button text is required.")
            sections = self._load_json_list("sections_json")
            total_rows = 0
            seen_ids = set()
            clean_sections = []
            for section in sections:
                section_title = str(section.get("title", "")).strip()
                rows = section.get("rows") or []
                if not section_title or not isinstance(rows, list) or not rows:
                    self.add_error("sections_json", "Section title and rows are required.")
                    continue
                if len(section_title) > 24:
                    self.add_error("sections_json", "Section title maximum is 24 characters.")
                clean_rows = []
                for row in rows:
                    total_rows += 1
                    row_id = str(row.get("id", "")).strip()
                    title = str(row.get("title", "")).strip()
                    description = str(row.get("description", "")).strip()
                    if not row_id or not title:
                        self.add_error("sections_json", "Row ID and title are required.")
                        continue
                    if row_id in seen_ids:
                        self.add_error("sections_json", "Row IDs must be unique.")
                    seen_ids.add(row_id)
                    if len(row_id) > 200:
                        self.add_error("sections_json", "Row ID maximum is 200 characters.")
                    if len(title) > 24:
                        self.add_error("sections_json", "Row title maximum is 24 characters.")
                    if len(description) > 72:
                        self.add_error("sections_json", "Row description maximum is 72 characters.")
                    clean_row = {"id": row_id, "title": title}
                    if description:
                        clean_row["description"] = description
                    clean_rows.append(clean_row)
                clean_sections.append({"title": section_title, "rows": clean_rows})
            if not clean_sections or total_rows > 10:
                self.add_error("sections_json", "Add from 1 to 10 rows total.")
            payload["button"] = button_text
            payload["sections"] = clean_sections

        elif message_type == "cta_url":
            display_text = (cleaned.get("cta_display_text") or "").strip()
            url = (cleaned.get("cta_url") or "").strip()
            if not display_text:
                self.add_error("cta_display_text", "Button text is required.")
            if not url:
                self.add_error("cta_url", "URL is required.")
            payload["display_text"] = display_text
            payload["url"] = url

        elif message_type == "voice_call":
            display_text = (cleaned.get("voice_call_display_text") or "").strip()
            ttl_minutes = cleaned.get("voice_call_ttl_minutes")
            call_payload = (cleaned.get("voice_call_payload") or "").strip()
            if display_text:
                payload["display_text"] = display_text
            if ttl_minutes:
                payload["ttl_minutes"] = ttl_minutes
            if call_payload:
                payload["call_payload"] = call_payload

        elif message_type == "call_permission_request":
            pass

        self.cleaned_payload = payload
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.payload = self.cleaned_payload
        if commit:
            instance.save()
        return instance

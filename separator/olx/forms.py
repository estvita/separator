from django import forms

from .models import OlxCategory, OlxCity, OlxDistrict, OlxRegion


class OlxAdvertForm(forms.Form):
    title = forms.CharField(max_length=150)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    category = forms.ModelChoiceField(queryset=OlxCategory.objects.none())
    advertiser_type = forms.ChoiceField(
        choices=(("private", "private"), ("business", "business")),
        initial="private",
    )
    contact_name = forms.CharField(max_length=255)
    contact_phone = forms.CharField(max_length=255, required=False)
    region = forms.ModelChoiceField(queryset=OlxRegion.objects.none())
    city = forms.ModelChoiceField(queryset=OlxCity.objects.none())
    district = forms.ModelChoiceField(queryset=OlxDistrict.objects.none(), required=False)
    images = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="URL картинок, каждый с новой строки",
    )
    pushup_interval_days = forms.IntegerField(min_value=0, initial=0)
    pushup_time = forms.TimeField(
        required=False,
        initial="12:00",
        widget=forms.TimeInput(attrs={"type": "time"}),
    )

    def __init__(self, *args, client_domain=None, **kwargs):
        super().__init__(*args, **kwargs)
        if client_domain:
            self.fields["category"].queryset = OlxCategory.objects.filter(
                client_domain=client_domain,
                is_leaf=True,
            ).order_by("name")
            self.fields["region"].queryset = OlxRegion.objects.filter(
                client_domain=client_domain,
            ).order_by("name")
            self.fields["city"].queryset = OlxCity.objects.filter(
                client_domain=client_domain,
            ).select_related("region").order_by("name")
            self.fields["district"].queryset = OlxDistrict.objects.filter(
                client_domain=client_domain,
            ).select_related("city").order_by("city__name", "name")

        for field in self.fields.values():
            css_class = "form-control"
            if isinstance(field.widget, forms.CheckboxInput):
                css_class = "form-check-input"
            field.widget.attrs.setdefault("class", css_class)

    def clean_city(self):
        city = self.cleaned_data.get("city")
        region = self.cleaned_data.get("region")
        if city and region and city.region_id and city.region_id != region.id:
            raise forms.ValidationError("City does not belong to selected region.")
        return city

    def clean_district(self):
        district = self.cleaned_data.get("district")
        city = self.cleaned_data.get("city")
        if district and city and district.city_id != city.id:
            raise forms.ValidationError("District does not belong to selected city.")
        return district

    def build_payload(self):
        data = self.cleaned_data
        payload = {
            "title": data["title"],
            "description": data["description"],
            "category_id": data["category"].olx_id,
            "advertiser_type": data["advertiser_type"],
            "contact": {
                "name": data["contact_name"],
                "phone": data.get("contact_phone") or "",
            },
            "location": {
                "city_id": data["city"].olx_id,
            },
            "attributes": [],
        }
        if data.get("district"):
            payload["location"]["district_id"] = data["district"].olx_id
        if data.get("images"):
            payload["images"] = [
                {"url": url.strip()}
                for url in data["images"].splitlines()
                if url.strip()
            ]
        return payload


def olx_advert_initial(advert):
    payload = advert.payload or {}
    contact = payload.get("contact") or {}
    images = payload.get("images") or []
    city = advert.city
    return {
        "title": payload.get("title") or advert.title,
        "description": payload.get("description") or "",
        "category": advert.category_id,
        "advertiser_type": payload.get("advertiser_type") or "private",
        "contact_name": contact.get("name") or "",
        "contact_phone": contact.get("phone") or "",
        "region": city.region_id if city else None,
        "city": advert.city_id,
        "district": advert.district_id,
        "images": "\n".join(image.get("url", "") for image in images if image.get("url")),
        "pushup_interval_days": advert.pushup_interval_days,
        "pushup_time": advert.pushup_time,
    }

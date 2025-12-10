from django.db import models
from modelcluster.fields import ParentalKey
from wagtail.models import (
    DraftStateMixin,
    PreviewableMixin,
    RevisionMixin,
    TranslatableMixin,
    Page
)
from wagtail.models import Site as WagtailSite
from django.contrib.sites.models import Site as DjangoSite
from wagtail.fields import (
    RichTextField,
    StreamField,
)
from wagtail.admin.panels import (
    FieldPanel, FieldRowPanel,
    PublishingPanel, InlinePanel, MultiFieldPanel
)
from wagtail.contrib.forms.models import AbstractEmailForm, AbstractFormField
from wagtail.contrib.forms.panels import FormSubmissionsPanel


from wagtail.contrib.typed_table_block.blocks import TypedTableBlock
from wagtail.snippets.models import register_snippet
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock
from wagtailcodeblock.blocks import CodeBlock

from separator.tariff.models import Tariff, Service


class HomePage(Page):
    body = RichTextField(blank=True)
    menu_title = models.CharField(blank=True, max_length=150)

    content_panels = Page.content_panels + [
        FieldPanel('body'),
    ]
    promote_panels = Page.promote_panels + [
        FieldPanel("menu_title"),
    ]

    def get_menu_title(self):
        return self.menu_title if self.menu_title else self.title


class ArticlePage(Page):
    menu_title = models.CharField(blank=True, max_length=150)
    body = StreamField([
        ("rich_text", blocks.RichTextBlock()),
        ("code", CodeBlock(label="Code")),
        ('html', blocks.RawHTMLBlock()),
        ("table", TypedTableBlock([
            ('text', blocks.CharBlock(required=False)),
            ('numeric', blocks.FloatBlock(required=False)),
            ('rich_text', blocks.RichTextBlock(required=False)),
            ('image', ImageChooserBlock()),
            ('url', blocks.URLBlock(required=False)),
            ('flag', blocks.BooleanBlock(required=False)),
            ('datetime', blocks.DateTimeBlock(required=False)),
            ('date', blocks.DateBlock(required=False)),
            ('time', blocks.TimeBlock(required=False)),
            ('email', blocks.EmailBlock(required=False)),
            ('html', blocks.RawHTMLBlock(required=False)),
            ('page', blocks.PageChooserBlock()),
        ])),
    ], blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("body"),
    ]
    promote_panels = Page.promote_panels + [
        FieldPanel("menu_title"),
    ]

    def get_menu_title(self):
        return self.menu_title if self.menu_title else self.title


class TariffPage(Page):
    menu_title = models.CharField(blank=True, max_length=150)
    body = StreamField([
        ("rich_text", blocks.RichTextBlock()),
    ], blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("body"),
    ]

    promote_panels = Page.promote_panels + [
        FieldPanel("menu_title"),
    ]

    def get_menu_title(self):
        return self.menu_title if self.menu_title else self.title

    def get_context(self, request):
        context = super().get_context(request)
        wagtail_site = WagtailSite.find_for_request(request)
        django_site = DjangoSite.objects.get(domain=wagtail_site.hostname)
        context["tariffs"] = Tariff.objects.filter(site=django_site)
        context["services"] = Service.objects.filter(tariffs__site=django_site).distinct()
        return context


@register_snippet
class FooterText(
    DraftStateMixin,
    RevisionMixin,
    PreviewableMixin,
    TranslatableMixin,
    models.Model,
):
    site = models.OneToOneField(WagtailSite, on_delete=models.CASCADE, related_name="footer_text")
    body = StreamField([
        ("text", blocks.RichTextBlock()),
    ], blank=True, use_json_field=True)
    panels = [
        FieldPanel("site"),
        FieldPanel("body"),
        PublishingPanel(),
    ]
    def __str__(self):
        return f"Footer Text for {self.site.hostname}" if self.site else f"Footer Text {self.id}"
    class Meta(TranslatableMixin.Meta):
        verbose_name_plural = "Footer Text"


class FormField(AbstractFormField):
    page = ParentalKey('FormPage', on_delete=models.CASCADE, related_name='form_fields')


class FormPage(AbstractEmailForm):
    intro = RichTextField(blank=True)
    thank_you_text = RichTextField(blank=True)
    body = StreamField([
        ("rich_text", blocks.RichTextBlock()),
        ("table", TypedTableBlock([
            ('text', blocks.CharBlock(required=False)),
            ('numeric', blocks.FloatBlock(required=False)),
            ('image', ImageChooserBlock()),
            ('url', blocks.URLBlock(required=False)),
            ('flag', blocks.BooleanBlock(required=False)),
            ('datetime', blocks.DateTimeBlock(required=False)),
            ('date', blocks.DateBlock(required=False)),
            ('time', blocks.TimeBlock(required=False)),
            ('email', blocks.EmailBlock(required=False)),
            ('page', blocks.PageChooserBlock()),
        ])),
    ], blank=True, use_json_field=True)

    content_panels = AbstractEmailForm.content_panels + [
        FormSubmissionsPanel(),
        FieldPanel('intro'),
        FieldPanel('body'),
        InlinePanel('form_fields'),
        FieldPanel('thank_you_text'),
        MultiFieldPanel([
            FieldRowPanel([
                FieldPanel('from_address', classname="col6"),
                FieldPanel('to_address', classname="col6"),
            ]),
            FieldPanel('subject'),
        ], "Email"),
    ]

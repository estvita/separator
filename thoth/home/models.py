from django.db import models
from wagtail.models import (
    DraftStateMixin,
    PreviewableMixin,
    RevisionMixin,
    TranslatableMixin,
    Page, Site
)
from wagtail.fields import (
    RichTextField,
    StreamField,
)
from wagtail.admin.panels import (
    FieldPanel,
    PublishingPanel,
)

from wagtail.snippets.models import register_snippet
from wagtail.blocks import RichTextBlock
from wagtailcodeblock.blocks import CodeBlock

from thoth.tariff.models import Tariff, Service


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
        ("rich_text", RichTextBlock()),
        ("code", CodeBlock(label="Code")),
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
        ("rich_text", RichTextBlock()),
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
        context["tariffs"] = Tariff.objects.all()
        context["services"] = Service.objects.all()
        return context


@register_snippet
class FooterText(
    DraftStateMixin,
    RevisionMixin,
    PreviewableMixin,
    TranslatableMixin,
    models.Model,
):
    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="footer_text")
    body = StreamField([
        ("text", RichTextBlock()),
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
from django.db import models
from wagtail.models import (
    DraftStateMixin,
    PreviewableMixin,
    RevisionMixin,
    TranslatableMixin,
    Page,
)
from wagtail.fields import (
    RichTextField,
    StreamField,
)
from wagtail.admin.panels import (
    FieldPanel,
    MultiFieldPanel,
    PublishingPanel,
)
from wagtail.contrib.settings.models import (
    BaseGenericSetting,
    register_setting,
)

from wagtail.snippets.models import register_snippet

from wagtail.blocks import RichTextBlock
from wagtailcodeblock.blocks import CodeBlock

from wagtailseo.models import SeoMixin, SeoType

from thoth.tariff.models import Tariff, Service


class HomePage(SeoMixin, Page):
    body = RichTextField(blank=True)
    menu_title = models.CharField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel('body'),
    ]
    promote_panels = Page.promote_panels + [
        FieldPanel("menu_title"),
    ] + SeoMixin.seo_panels
    seo_content_type = SeoType.ARTICLE

    def get_menu_title(self):
        return self.menu_title if self.menu_title else self.title


class ArticlePage(SeoMixin, Page):
    menu_title = models.CharField(blank=True)
    body = StreamField([
        ("rich_text", RichTextBlock()),
        ("code", CodeBlock(label="Code")),
    ], blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("body"),
    ]
    promote_panels = Page.promote_panels + [
        FieldPanel("menu_title"),
    ] + SeoMixin.seo_panels

    seo_content_type = SeoType.ARTICLE

    def get_menu_title(self):
        return self.menu_title if self.menu_title else self.title


class TariffPage(SeoMixin, Page):
    menu_title = models.CharField(blank=True)
    body = StreamField([
        ("rich_text", RichTextBlock()),
    ], blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("body"),
    ]

    promote_panels = Page.promote_panels + [
        FieldPanel("menu_title"),
    ] + SeoMixin.seo_panels

    def get_menu_title(self):
        return self.menu_title if self.menu_title else self.title

    def get_context(self, request):
        context = super().get_context(request)
        context["tariffs"] = Tariff.objects.all()
        context["services"] = Service.objects.all()
        return context


@register_setting
class NavigationSettings(BaseGenericSetting):
    github_url = models.URLField(verbose_name="GitHub URL", blank=True)

    panels = [
        MultiFieldPanel(
            [
                FieldPanel("github_url"),
            ],
            "Social settings",
        )
    ]


@register_snippet
class FooterText(
    DraftStateMixin,
    RevisionMixin,
    PreviewableMixin,
    TranslatableMixin,
    models.Model,
):
    body = StreamField([
        ("text", RichTextBlock()),
    ], blank=True, use_json_field=True)

    panels = [
        FieldPanel("body"),
        PublishingPanel(),
    ]

    def __str__(self):
        return f"Footer Text {self.id}"

    class Meta(TranslatableMixin.Meta):
        verbose_name_plural = "Footer Text"
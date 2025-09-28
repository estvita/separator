from django import template
from wagtail.models import Site

from thoth.home.models import FooterText

register = template.Library()


@register.inclusion_tag("includes/footer_text.html", takes_context=True)
def get_footer_text(context):
    request = context["request"]
    site = Site.find_for_request(request)
    instance = FooterText.objects.filter(site=site).first()
    return {
        "footer_blocks": instance.body if instance else []
    }


@register.simple_tag(takes_context=True)
def get_site_root(context):
    request = context.get("request")
    if not request:
        return ""
    return Site.find_for_request(request).root_page
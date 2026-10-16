from django import template


register = template.Library()


@register.filter
def space_thousands(value):
    if value is None:
        return ""

    try:
        return format(value, ",").replace(",", "\u00a0")
    except (TypeError, ValueError):
        return value

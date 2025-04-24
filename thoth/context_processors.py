from django.conf import settings

def footer_links_visibility(request):
    return {
        'SHOW_FOOTER_LINKS': getattr(settings, 'SHOW_FOOTER_LINKS', True)
    }
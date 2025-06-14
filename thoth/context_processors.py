from django.conf import settings

def footer_links_visibility(request):
    return {
        'SHOW_FOOTER_LINKS': getattr(settings, 'SHOW_FOOTER_LINKS', True)
    }

def app_links(request):
    return {
        'link_store': settings.B24_LINK_STORE,
        'link_waba': settings.B24_LINK_WABA,
        'link_waweb': settings.B24_LINK_WAWEB,
        'link_olx': settings.B24_LINK_OLX,
    }
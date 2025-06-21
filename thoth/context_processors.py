from django.conf import settings

def footer_links_visibility(request):
    return {
        'SHOW_FOOTER_LINKS': getattr(settings, 'SHOW_FOOTER_LINKS', True)
    }

def installed_apps(request):
    return {"INSTALLED_APPS": settings.INSTALLED_APPS}
import os
from django.conf import settings

def footer_links_visibility(request):
    hostname = request.get_host().split(':')[0].lower()
    show_footer_links = True

    if 'wagtail.sites' in settings.INSTALLED_APPS or 'wagtailcore' in settings.INSTALLED_APPS:
        try:
            from wagtail.models import Site
            exists = Site.objects.filter(hostname__iexact=hostname).exists()
            if exists:
                show_footer_links = False
        except Exception:
            pass

    return {
        'SHOW_FOOTER_LINKS': show_footer_links
    }

def installed_apps(request):
    return {"INSTALLED_APPS": settings.INSTALLED_APPS}

def site_name(request):
    name = os.environ.get('SITE_NAME', 'separator.biz')
    if 'wagtail' in settings.INSTALLED_APPS or 'wagtail.core' in settings.INSTALLED_APPS:
        try:
            from wagtail.models import Site
            site = Site.find_for_request(request)
            if site and getattr(site, 'site_name', None):
                name = site.site_name
        except Exception:
            pass
    return {'SITE_NAME': name}
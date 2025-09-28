import os
from django.conf import settings

def footer_links_visibility(request):
    hostname = request.get_host().split(':')[0].lower()
    blocked = ['gulin.kz', 'separator.biz']
    is_blocked = any(
        hostname == base or hostname.endswith('.' + base)
        for base in blocked
    )
    return {
        'SHOW_FOOTER_LINKS': not is_blocked
    }

def installed_apps(request):
    return {"INSTALLED_APPS": settings.INSTALLED_APPS}

def site_name(request):
    name = os.environ.get('SITE_NAME', 'gulin.kz')
    try:
        from wagtail.models import Site
        site = Site.find_for_request(request)
        if site and getattr(site, 'site_name', None):
            name = site.site_name
    except ImportError:
        pass
    return {'SITE_NAME': name}
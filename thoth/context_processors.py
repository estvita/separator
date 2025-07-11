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
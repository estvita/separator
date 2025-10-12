from wagtail.models import Site

def internal_domains(request):
    domains = list(Site.objects.values_list("hostname", flat=True))
    return {'internal_domains': domains}
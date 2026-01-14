# ruff: noqa
import os
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include
from django.urls import path
from django.views.generic import TemplateView
from django.views import defaults as default_views
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token

from separator.bitrix.views import log_and_serve_temp_file
from django.conf.urls.i18n import i18n_patterns

urlpatterns = [
    # Django Admin, use {% url 'admin:index' %}
    path(settings.ADMIN_URL, admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("users/", include("separator.users.urls", namespace="users")),
    path("accounts/", include("allauth.urls")),
    path("waba/", include("separator.waba.urls")),
    path('waweb/', include('separator.waweb.urls')),
    path('bitbot/', include('separator.bitbot.urls')),
    # ...
    # Logging temp file access
    path("media/temp/<path:path>", log_and_serve_temp_file),
    # Media files
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

if settings.ASTERX_SERVER:
    urlpatterns += [
        path('asterx/', include('separator.asterx.urls')),
    ]
else:
    urlpatterns += [
        path('asterx/', TemplateView.as_view(template_name="asterx/disabled.html")),
    ]

# API URLS
urlpatterns += [
    # API base url
    path("api/", include("config.api_router")),
    # DRF auth token
    path("api/auth-token/", obtain_auth_token),
    path("api/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
    path("", include("separator.bitrix.urls")),
    path("", include("separator.olx.urls")),
]

if os.environ.get("DJANGO_SETTINGS_MODULE") == "config.settings.vendor":
    from wagtail import urls as wagtail_urls
    from wagtail.admin import urls as wagtailadmin_urls
    from wagtail.documents import urls as wagtaildocs_urls
    from wagtail.contrib.sitemaps.views import sitemap
    from django.conf.urls.i18n import i18n_patterns

    urlpatterns += [
        path('hijack/', include('hijack.urls')),
        path(settings.WAGTAIL_CMS_URL, include(wagtailadmin_urls)),
        path("documents/", include(wagtaildocs_urls)),
        path('sitemap.xml', sitemap),
    ]
    urlpatterns += i18n_patterns(
        path("", include(wagtail_urls)),
        prefix_default_language=False,
    )
    
else:
    urlpatterns = [
        path("", TemplateView.as_view(template_name="base.html"), name="home"),
    ] + urlpatterns

if settings.DEBUG:
    # This allows the error pages to be debugged during development, just visit
    # these url in browser to see how these error pages look like.
    urlpatterns += [
        path(
            "400/",
            default_views.bad_request,
            kwargs={"exception": Exception("Bad Request!")},
        ),
        path(
            "403/",
            default_views.permission_denied,
            kwargs={"exception": Exception("Permission Denied")},
        ),
        path(
            "404/",
            default_views.page_not_found,
            kwargs={"exception": Exception("Page not Found")},
        ),
        path("500/", default_views.server_error),
    ]
    if "debug_toolbar" in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns


admin.site.site_header = "separator.biz"
admin.site.site_title = "Admin Portal"
admin.site.index_title = "Welcome to Admin Portal"
# ruff: noqa
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include
from django.urls import path
from django.views import defaults as default_views
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token
from django.contrib.flatpages.views import flatpage

from wagtail.admin import urls as wagtailadmin_urls
from wagtail import urls as wagtail_urls
from wagtail.documents import urls as wagtaildocs_urls
from wagtail.contrib.sitemaps.views import sitemap
from django.conf.urls.i18n import i18n_patterns


urlpatterns = [
    # path("", flatpage, {'url': '/'}, name="home"),
    # path("pages/", include("django.contrib.flatpages.urls")),
    # Django Admin, use {% url 'admin:index' %}
    path(settings.ADMIN_URL, admin.site.urls),
    path('sitemap.xml', sitemap),
    path("users/", include("thoth.users.urls", namespace="users")),
    path("accounts/", include("allauth.urls")),
    path("waba/", include("thoth.waba.urls")),
    path("chat/", include("thoth.chatwoot.urls")),
    path('waweb/', include('thoth.waweb.urls', namespace='waweb')),
    path('bots/', include('thoth.bot.urls_bot', namespace='bot')),
    path('voices/', include('thoth.bot.urls_voice', namespace='voice')),
    # Your stuff: custom urls includes go here
    # ...
    path(settings.WAGTAIL_CMS_URL, include(wagtailadmin_urls)),
    path('documents/', include(wagtaildocs_urls)),
    # Media files
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
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
    path("", include("thoth.bitrix.urls")),
    path("", include("thoth.olx.urls")),
]

urlpatterns += i18n_patterns(
    path("", include(wagtail_urls)),
    prefix_default_language=False,
)

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


admin.site.site_header = "Admin"
admin.site.site_title = "Admin Portal"
admin.site.index_title = "Welcome to gulin.kz"
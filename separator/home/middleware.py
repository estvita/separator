from urllib.parse import urlparse
from django import http
from wagtail.contrib.redirects.middleware import RedirectMiddleware, get_redirect
from wagtail.contrib.redirects import models

class FlexibleRedirectMiddleware(RedirectMiddleware):
    """
    Subclass of Wagtail's RedirectMiddleware that handles trailing slashes flexibly.
    If a redirect is not found for the exact path, it tries toggling the trailing slash.
    """
    def process_response(self, request, response):
        # Let the standard middleware try to find a match first
        response = super().process_response(request, response)
        
        # If already redirected, we are done
        if 300 <= response.status_code < 400:
            return response

        # Logic to handle redirects even if page exists (200) OR if 404 was not caught by strict match
        if response.status_code in [200, 404]:
            full_path = models.Redirect.normalise_path(request.get_full_path())
            
            # If status is 200, we need to manually check for an exact match Redirect
            # because standard RedirectMiddleware skips non-404 responses.
            if response.status_code == 200:
                redirect = get_redirect(request, full_path)
                if redirect:
                    if redirect.link is None:
                        return response
                    if redirect.is_permanent:
                        return http.HttpResponsePermanentRedirect(redirect.link)
                    else:
                        return http.HttpResponseRedirect(redirect.link)

            # If we are here, we either have a 404 (strict failed) or 200 (strict failed)
            # Now try flexible matching (slash toggling)
            parsed = urlparse(full_path)
            path_root = parsed.path
            query = parsed.query
            
            candidates = []
            
            # If path ends with slash, try without. If it doesn't, try with.
            if path_root.endswith('/'):
                # Only strip if not root '/'
                if len(path_root) > 1:
                    alt_root = path_root.rstrip('/')
                    if query:
                        candidates.append(alt_root + '?' + query)
                    candidates.append(alt_root)
            else:
                alt_root = path_root + '/'
                if query:
                    candidates.append(alt_root + '?' + query)
                candidates.append(alt_root)
                
            # Check candidates
            for path in candidates:
                # We use Wagtail's helper to find the redirect
                redirect = get_redirect(request, path)
                if redirect:
                    if redirect.link is None:
                        return response
                    
                    if redirect.is_permanent:
                        return http.HttpResponsePermanentRedirect(redirect.link)
                    else:
                        return http.HttpResponseRedirect(redirect.link)
                        
        return response

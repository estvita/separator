from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.urls import reverse
from separator.decorators import user_message


BITRIX_EMBED_VIEW_NAMES = {
    "app_settings",
    "process_placement",
    "process_placement_legacy",
}


class CheckPhoneNumberMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _is_bitrix_embed_request(request):
        if request.POST.get("PLACEMENT") or request.POST.get("PLACEMENT_OPTIONS"):
            return True
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False
        return match.view_name in BITRIX_EMBED_VIEW_NAMES

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not user.is_superuser:
            profile_url = reverse("users:update")
            if (
                not user.phone_number
                and request.path not in [profile_url, '/app-install/']
                and not self._is_bitrix_embed_request(request)
            ):
                if not request.session.get("redirect_after_profile_update"):
                    request.session["redirect_after_profile_update"] = request.path
                user_message(request, "user_phone_number", "error")
                return redirect(profile_url)
        return self.get_response(request)

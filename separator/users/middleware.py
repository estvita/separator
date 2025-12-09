from django.shortcuts import redirect
from django.urls import reverse
from separator.decorators import user_message

class CheckPhoneNumberMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not user.is_superuser:
            profile_url = reverse("users:update")
            if not user.phone_number and request.path not in [profile_url, '/app-install/']:
                if not request.session.get("redirect_after_profile_update"):
                    request.session["redirect_after_profile_update"] = request.path
                user_message(request, "user_phone_number", "error")
                return redirect(profile_url)
        return self.get_response(request)
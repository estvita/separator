from django.contrib import messages
from django.shortcuts import redirect
from django.conf import settings
from separator.users.models import Message


def user_message(request, code=None):
    if code:
        message = Message.objects.filter(code=code).first()
        if message:
            messages.warning(request, message.message)

def login_message_required(code=None):
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            user_message(request, code)
            if not request.user.is_authenticated:
                return redirect(f'{settings.LOGIN_URL}?next={request.get_full_path()}')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
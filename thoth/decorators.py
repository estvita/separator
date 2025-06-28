from django.contrib import messages
from django.shortcuts import redirect
from django.conf import settings
from thoth.users.models import Message

def login_message_required(code=None):
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            if code:
                message = Message.objects.filter(code=code).first()
                if message:
                    messages.warning(request, message.message)
            if not request.user.is_authenticated:
                return redirect(f'{settings.LOGIN_URL}?next={request.path}')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
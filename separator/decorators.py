from django.contrib import messages
from django.shortcuts import redirect
from django.conf import settings
from django.contrib.sites.models import Site
from separator.users.models import Message


def user_message(request, code=None, message_type='info'):
    if code:
        host = request.get_host().split(':')[0]
        site = Site.objects.filter(domain=host).first()
        message = Message.objects.filter(code=code, site=site).first()
        if message:
            msg_func = getattr(messages, message_type, messages.warning)
            msg_func(request, message.message)


def login_message_required(code=None):
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            user_message(request, code)
            if not request.user.is_authenticated:
                return redirect(f'{settings.LOGIN_URL}?next={request.get_full_path()}')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
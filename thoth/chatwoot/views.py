from django.shortcuts import render

# Create your views here.
from django.shortcuts import redirect

import thoth.chatwoot.utils as utils

def chat_sso_redirect(request):
    user = request.user
    sso_link = utils.get_sso_link(user)
    return redirect(sso_link)

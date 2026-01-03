from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone
from django.db import transaction
from django.shortcuts import get_object_or_404
from separator.waba.models import Phone
import separator.waba.tasks as waba_tasks

class ExtViewSet(GenericViewSet):
    @action(detail=False, methods=['get'])
    def check_status(self, request):
        if not request.user.is_staff:
            return Response({"error": "permission denied"}, status=403)

        phone = request.GET.get('phone')
        if not phone:
            return Response({"error": "phone is required."}, status=400)
        phone_obj = get_object_or_404(Phone, phone=phone)
        disabled = False
        # if phone_obj.date_end and timezone.now() > phone_obj.date_end:
        #     disabled = True
        if not phone_obj.sip_extensions or phone_obj.calling == "disabled":
            return Response({"error": "disabled"}, status=404)
        ext = phone_obj.sip_extensions
        if ext.date_end and timezone.now() > ext.date_end:
            return Response({"error": "payment required"}, status=402)
        #     disabled = True
        # if disabled:
        #     phone_obj.calling = "disabled"
        #     phone_obj.call_dest = "disabled"
        #     phone_obj.save()
        #     transaction.on_commit(lambda: waba_tasks.call_management.delay(phone_obj.id))
        return Response("success")
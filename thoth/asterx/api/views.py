from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from django.utils import timezone
from thoth.asterx.models import Server
from thoth.bitrix.crest import refresh_token
from thoth.bitrix.models import Bitrix, Credential

class AsterxHandler(GenericViewSet):
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'])
    def refresh_token(self, request):
        server_id = request.data.get('server_id')
        member_id = request.data.get('member_id')
        access_token = request.data.get('access_token')
        if not server_id or not member_id or not access_token:
            return Response({"error": "ID is required."}, status=400)
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return Response({"error": "Server not found."}, status=404)
        
        date_end = server.date_end
        if date_end and timezone.now() > date_end:
            return Response({"error": "Server license has expired."}, status=402)
        
        try:
            Bitrix.objects.get(member_id=member_id)
        except Bitrix.DoesNotExist:
            return Response({"error": "B24 not found."}, status=403)
        
        try:
            credential = Credential.objects.get(access_token=access_token)
        except Credential.DoesNotExist:
            return Response({"error": "Credential not found."}, status=403)
        
        if refresh_token(credential):
            credential.refresh_from_db()
            return Response({"access_token": credential.access_token})

        return Response({"status": "ok"})
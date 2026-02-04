from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from django.utils import timezone
from separator.asterx.models import Server
from separator.bitrix.crest import refresh_token
from separator.bitrix.models import Bitrix

class AsterxHandler(GenericViewSet):
    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def refresh_token(self, request):
        server_id = request.data.get('server_id')
        member_id = request.data.get('member_id')
        if not server_id or not member_id:
            return Response({"error": "ID is required."}, status=400)
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return Response({"error": "Server not found."}, status=404)
        
        date_end = server.date_end
        if date_end and timezone.now() > date_end:
            return Response({"error": "Server license has expired."}, status=402)
        
        try:
            portal = Bitrix.objects.get(member_id=member_id)
        except Bitrix.DoesNotExist:
            return Response({"error": "B24 not found."}, status=403)
        
        active_users = portal.users.filter(active=True)
        for user in active_users:
            credential = user.credentials.filter(app_instance=server.settings.app_instance).first()
            if not credential:
                continue        
            if refresh_token(credential):
                credential.refresh_from_db()
                return Response({"access_token": credential.access_token})

        return Response({"status": "ok"})
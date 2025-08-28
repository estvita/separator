import json
from django.utils import timezone
from rest_framework.authtoken.models import Token
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Server, Context
from thoth.bitrix.models import User as BitrixUser, Credential

class ServerAuthConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        query = self.scope['query_string'].decode()
        server_id = None
        for part in query.split('&'):
            if part.startswith('server_id='):
                server_id = part.split('=')[1]
                break
        if not server_id:
            await self.close()
            return

        self.server_id = server_id
        self.server = await database_sync_to_async(
            lambda: Server.objects.filter(id=server_id).first()
        )()
        if not self.server:
            await self.close()
            return

        date_end = self.server.date_end
        if date_end and timezone.now() > date_end:
            await self.accept()
            await self.send(text_data=json.dumps({
                "error": "Server license has expired."
            }))
            await self.close()
            return

        await self.accept()
        # ОЖИДАЕМ данные от клиента — переключаемся на receive()

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data)
            contexts = data.get("contexts")
        except Exception:
            print("Invalid message")
            await self.close()
            return

        # 3. Проверить/обработать клиентские данные (core_info)
        # ожидаем хотя бы entity_id и pbx_uuid
        entity_id = data.get("entity_id")
        pbx_uuid = data.get("pbx_uuid")
        version = data.get("version")
        system = data.get("system")

        if contexts:
            await self.process_contexts(contexts)
        self.group_name = f"server_{self.server_id}"
        if not self.server.setup_complete:
            # 4. setup_complete == False: записать поля и отметить сервер
            await self.save_server_data(
                version, system, entity_id, pbx_uuid
            )
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.mark_setup_complete(self.server_id)
            server_data = await self.get_server_and_instance_data(self.server_id)
            if server_data:
                await self.send(text_data=json.dumps(server_data))
        else:
            # 5. setup_complete == True — сверить entity_id/pbx_uuid
            if (str(self.server.entity_id) == str(entity_id) and
                str(self.server.pbx_uuid) == str(pbx_uuid)):
                await self.save_server_data(version, system)
                await self.channel_layer.group_add(self.group_name, self.channel_name)
                server_data = await self.get_server_and_instance_data(self.server_id)
                if server_data:
                    await self.send(text_data=json.dumps(server_data))

            else:
                await self.send(text_data=json.dumps(
                    {"error": "This license is linked to a different server."}
                ))
                await self.close()

    @database_sync_to_async
    def process_contexts(self, contexts):
        for ctx in contexts or []:
            context_name = ctx.get("context")
            endpoint = ctx.get("endpoint")
            if not context_name or not endpoint:
                continue
            obj, created = Context.objects.get_or_create(
                server=self.server, context=context_name,
                defaults={"endpoint": endpoint}
            )
            if not created:
                changes = False
                if obj.endpoint != endpoint:
                    obj.endpoint = endpoint
                    changes = True
                if changes:
                    obj.save()

    @database_sync_to_async
    def save_server_data(self, version, system, entity_id=None, pbx_uuid=None):
        self.server.version = version
        self.server.system = system
        if entity_id:
            self.server.entity_id = entity_id
        if pbx_uuid:
            self.server.pbx_uuid = pbx_uuid
        self.server.save()

    @database_sync_to_async
    def mark_setup_complete(self, server_id):
        Server.objects.filter(id=server_id).update(setup_complete=True)

    @database_sync_to_async
    def get_server_and_instance_data(self, server_id):
        server = self.server
        if not server.settings or not server.settings.app_instance:
            return None
        settings = server.settings
        instance = settings.app_instance
        portal = instance.portal

        bitrix_user = BitrixUser.objects.filter(owner=server.owner, bitrix=portal).first()
        access_token = None
        if bitrix_user:
            credential = Credential.objects.filter(
                user=bitrix_user,
                app_instance=instance,
            ).first()
            if credential:
                access_token = credential.access_token

        user_token = None
        if server.owner:
            try:
                user_token, _ = Token.objects.get_or_create(user=server.owner)
                user_token = user_token.key
            except Token.DoesNotExist:
                user_token = None

        return {
            "event": "setup_complete",
            "member_id": getattr(portal, "member_id", None),
            "protocol": getattr(portal, "protocol", None),
            "domain": getattr(portal, "domain", None),
            "access_token": access_token,
            "user_token": user_token,
            "show_card": settings.show_card,
            "crm_create": settings.crm_create,
            "vm_send": settings.vm_send,
            "smart_route": settings.smart_route,
            "default_user_id": settings.default_user_id,
        }
    
    
    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
    async def send_event(self, event):
        await self.send(text_data=json.dumps(event["message"]))
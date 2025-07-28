from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def send_call_info(id, payload):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"server_{id}",
        {"type": "send_event", "message": payload}
    )
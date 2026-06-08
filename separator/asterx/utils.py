import threading

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

# Limits concurrent async_to_sync calls to avoid OS thread exhaustion.
# Each call spawns a thread; without a cap, a load spike can exhaust the limit.
_SEND_SEMAPHORE = threading.Semaphore(50)


class SendCallInfoError(RuntimeError):
    """Raised when send_call_info cannot proceed (e.g. concurrency limit reached)."""


def send_call_info(id, payload):
    """Send a WebSocket event to trigger a PBX callback call.

    Retries should be applied at the call site (e.g. Celery autoretry_for=SendCallInfoError).
    """
    acquired = _SEND_SEMAPHORE.acquire(timeout=0)
    if not acquired:
        raise SendCallInfoError(
            f"send_call_info: concurrency limit reached, retrying later (server_id={id})"
        )
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"server_{id}",
            {"type": "send_event", "message": payload}
        )
    finally:
        _SEND_SEMAPHORE.release()
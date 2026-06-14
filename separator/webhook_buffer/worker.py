import base64
import json
import logging
import time
from urllib.parse import quote, urljoin

import redis
import requests

try:
    import requests_unixsocket
except ImportError:
    requests_unixsocket = None

from . import config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(
    config.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=config.REDIS_CONNECT_TIMEOUT,
    socket_timeout=config.REDIS_SOCKET_TIMEOUT,
    health_check_interval=config.REDIS_HEALTH_CHECK_INTERVAL,
)


def ensure_group():
    try:
        redis_client.xgroup_create(
            config.STREAM,
            config.FORWARD_GROUP,
            id="0",
            mkstream=True,
        )
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def is_no_group_error(exc):
    return "NOGROUP" in str(exc)


def build_url(path, query):
    base = config.FORWARD_URL.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    if query:
        return f"{url}?{query}"
    return url


def build_unix_socket_url(path, query):
    socket_path = quote(config.FORWARD_UNIX_SOCKET, safe="")
    url = f"http+unix://{socket_path}{path}"
    if query:
        return f"{url}?{query}"
    return url


def get_session():
    if not config.FORWARD_UNIX_SOCKET:
        return requests
    if requests_unixsocket is None:
        raise RuntimeError("requests-unixsocket is required for unix socket forwarding")
    return requests_unixsocket.Session()


def forward(data):
    body = base64.b64decode(data["body"])
    headers = json.loads(data.get("headers") or "{}")
    headers.pop("Content-Length", None)
    headers["X-Webhook-Buffered"] = "1"

    session = get_session()
    url = (
        build_unix_socket_url(data["path"], data.get("query") or "")
        if config.FORWARD_UNIX_SOCKET
        else build_url(data["path"], data.get("query") or "")
    )

    response = session.request(
        method=data["method"],
        url=url,
        data=body,
        headers=headers,
        timeout=config.FORWARD_TIMEOUT,
    )
    response.raise_for_status()


def ack_delete(message_id):
    pipe = redis_client.pipeline()
    pipe.xack(config.STREAM, config.FORWARD_GROUP, message_id)
    pipe.xdel(config.STREAM, message_id)
    pipe.execute()


def requeue_or_dead(message_id, data):
    retry_count = int(data.get("retry_count") or 0) + 1
    next_data = dict(data)

    if retry_count > config.FORWARD_MAX_RETRIES:
        next_data["failed_at"] = str(time.time())
        redis_client.xadd(config.DEAD_STREAM, next_data)
        ack_delete(message_id)
        logger.error("Moved webhook %s to dead stream", message_id)
        return

    next_data["retry_count"] = str(retry_count)
    time.sleep(min(60.0, retry_count * config.FORWARD_RETRY_DELAY))
    redis_client.xadd(config.STREAM, next_data)
    ack_delete(message_id)


def process_entry(message_id, data):
    try:
        forward(data)
    except Exception:
        logger.exception("Failed to forward webhook %s", message_id)
        requeue_or_dead(message_id, data)
        return

    ack_delete(message_id)
    logger.info("Forwarded webhook %s", message_id)


def claim_pending():
    result = redis_client.xautoclaim(
        config.STREAM,
        config.FORWARD_GROUP,
        config.FORWARD_CONSUMER,
        min_idle_time=config.FORWARD_PENDING_IDLE_MS,
        start_id="0-0",
        count=config.FORWARD_BATCH_SIZE,
    )
    if len(result) == 3:
        _, messages, _ = result
    else:
        _, messages = result
    return messages


def read_new_messages():
    return redis_client.xreadgroup(
        config.FORWARD_GROUP,
        config.FORWARD_CONSUMER,
        {config.STREAM: ">"},
        count=config.FORWARD_BATCH_SIZE,
        block=config.FORWARD_BLOCK_MS,
    )


def main():
    ensure_group()
    logger.info("Webhook forward worker started")

    while True:
        try:
            pending_messages = claim_pending()
            if pending_messages:
                for message_id, data in pending_messages:
                    process_entry(message_id, data)
                continue

            messages = read_new_messages()
        except redis.exceptions.TimeoutError:
            continue
        except redis.exceptions.ConnectionError:
            logger.exception("Redis connection error")
            time.sleep(1)
            continue
        except redis.exceptions.ResponseError as exc:
            if not is_no_group_error(exc):
                raise
            logger.warning("Redis stream group is missing, recreating it")
            ensure_group()
            continue

        for _, entries in messages:
            for message_id, data in entries:
                process_entry(message_id, data)


if __name__ == "__main__":
    main()

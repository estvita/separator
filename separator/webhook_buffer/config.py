import os


def env(name, default):
    return os.environ.get(name, default)


def env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


REDIS_URL = env("WEBHOOK_BUFFER_REDIS_URL", "redis://127.0.0.1:6381/0")
STREAM = env("WEBHOOK_BUFFER_STREAM", "webhook:incoming")
DEAD_STREAM = env("WEBHOOK_BUFFER_DEAD_STREAM", "webhook:dead")

PATHS = {
    path.strip()
    for path in env(
        "WEBHOOK_BUFFER_PATHS",
        "/api/bitrix/,/api/bitrix/sms/,/api/bitrix/bizproc/,/api/waba/",
    ).split(",")
    if path.strip()
}

FORWARD_URL = env("WEBHOOK_FORWARD_URL", "http://127.0.0.1:8000")
FORWARD_UNIX_SOCKET = env("WEBHOOK_FORWARD_UNIX_SOCKET", "")
FORWARD_GROUP = env("WEBHOOK_FORWARD_GROUP", "webhook-forwarders")
FORWARD_CONSUMER = env("WEBHOOK_FORWARD_CONSUMER", "worker-1")
FORWARD_TIMEOUT = env_float("WEBHOOK_FORWARD_TIMEOUT", 20.0)
FORWARD_MAX_RETRIES = env_int("WEBHOOK_FORWARD_MAX_RETRIES", 20)
FORWARD_RETRY_DELAY = env_float("WEBHOOK_FORWARD_RETRY_DELAY", 3.0)
FORWARD_BATCH_SIZE = env_int("WEBHOOK_FORWARD_BATCH_SIZE", 10)
FORWARD_BLOCK_MS = env_int("WEBHOOK_FORWARD_BLOCK_MS", 5000)
FORWARD_PENDING_IDLE_MS = env_int("WEBHOOK_FORWARD_PENDING_IDLE_MS", 60000)

REDIS_CONNECT_TIMEOUT = env_float("WEBHOOK_BUFFER_REDIS_CONNECT_TIMEOUT", 5.0)
REDIS_SOCKET_TIMEOUT = env_float(
    "WEBHOOK_BUFFER_REDIS_SOCKET_TIMEOUT",
    max(10.0, FORWARD_BLOCK_MS / 1000.0 + 5.0),
)
REDIS_HEALTH_CHECK_INTERVAL = env_int("WEBHOOK_BUFFER_REDIS_HEALTH_CHECK_INTERVAL", 30)

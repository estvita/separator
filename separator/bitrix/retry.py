import requests
from django.conf import settings
from django.db import InterfaceError, OperationalError, DatabaseError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ReadOnlyError as RedisReadOnlyError
from redis.exceptions import TimeoutError as RedisTimeoutError

_asterx_errors: tuple = ()
if getattr(settings, "ASTERX_SERVER", False):
    from separator.asterx.utils import SendCallInfoError
    _asterx_errors = (SendCallInfoError,)

TRANSIENT_ERRORS = (
    requests.RequestException,
    OperationalError,
    DatabaseError,
    InterfaceError,
    RedisConnectionError,
    RedisTimeoutError,
    RedisReadOnlyError,
    *_asterx_errors,
)

RETRY_KWARGS = {
    "autoretry_for": TRANSIENT_ERRORS,
    "retry_backoff": 5,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 5,
}

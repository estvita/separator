import requests
from kombu.exceptions import OperationalError as KombuOperationalError
from django.db import InterfaceError, OperationalError, DatabaseError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ReadOnlyError as RedisReadOnlyError
from redis.exceptions import TimeoutError as RedisTimeoutError


TRANSIENT_ERRORS = (
    requests.RequestException,
    OperationalError,
    DatabaseError,
    InterfaceError,
    RedisConnectionError,
    RedisTimeoutError,
    RedisReadOnlyError,
    KombuOperationalError,
)

RETRY_KWARGS = {
    "autoretry_for": TRANSIENT_ERRORS,
    "retry_backoff": 5,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 5,
}

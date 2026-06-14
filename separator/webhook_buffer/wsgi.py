import base64
import json
import time
from urllib.parse import parse_qsl

import redis

from . import config


redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "")
    path = environ.get("PATH_INFO", "")

    if method != "POST" or path not in config.PATHS:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]

    try:
        content_length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        content_length = 0

    body = environ["wsgi.input"].read(content_length)
    query = environ.get("QUERY_STRING", "")

    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            header = key[5:].replace("_", "-").title()
            headers[header] = value
    if environ.get("CONTENT_TYPE"):
        headers["Content-Type"] = environ["CONTENT_TYPE"]
    if environ.get("CONTENT_LENGTH"):
        headers["Content-Length"] = environ["CONTENT_LENGTH"]

    message = {
        "method": method,
        "path": path,
        "query": query,
        "query_params": json.dumps(parse_qsl(query, keep_blank_values=True)),
        "headers": json.dumps(headers),
        "body": base64.b64encode(body).decode("ascii"),
        "created_at": str(time.time()),
        "retry_count": "0",
    }

    try:
        redis_client.xadd(config.STREAM, message)
    except Exception:
        start_response("503 Service Unavailable", [("Content-Type", "text/plain")])
        return [b"redis unavailable"]

    start_response("202 Accepted", [("Content-Type", "text/plain")])
    return [b"ok"]


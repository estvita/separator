# Webhook Buffer

Webhook Buffer is a small standalone WSGI endpoint for accepting selected incoming webhooks before they reach the main Django application.

It is intended for short incidents when the main application temporarily cannot reach PostgreSQL, Redis Sentinel, or another required cluster dependency. The buffer does not import Django, does not use the database, and only writes incoming webhook data to a local Redis Stream.

## Flow

```text
nginx -> gunicorn -> webhook_buffer WSGI app -> local Redis Stream
worker -> main Gunicorn socket or HTTP endpoint -> Django application
```

The endpoint stores:

- HTTP method
- path
- query string
- headers
- raw request body encoded as base64
- creation timestamp
- retry counter

The worker reads messages from Redis, forwards them to the main application, and acknowledges them only after a successful HTTP response. Failed messages are requeued with a delay. After the retry limit is exceeded, messages are moved to the dead stream.

## Buffered Paths

Default buffered paths:

```text
/api/bitrix/
/api/bitrix/sms/
/api/bitrix/bizproc/
/api/waba/
```

Only `POST` requests are accepted. Other methods return `404`.

## Environment Variables

All variables have defaults and can be overridden through environment variables.

```env
WEBHOOK_BUFFER_REDIS_URL=redis://127.0.0.1:6381/0
WEBHOOK_BUFFER_REDIS_CONNECT_TIMEOUT=5
WEBHOOK_BUFFER_REDIS_SOCKET_TIMEOUT=10
WEBHOOK_BUFFER_REDIS_HEALTH_CHECK_INTERVAL=30
WEBHOOK_BUFFER_STREAM=webhook:incoming
WEBHOOK_BUFFER_DEAD_STREAM=webhook:dead
WEBHOOK_BUFFER_PATHS=/api/bitrix/,/api/bitrix/sms/,/api/bitrix/bizproc/,/api/waba/

WEBHOOK_FORWARD_URL=http://127.0.0.1:8000
WEBHOOK_FORWARD_UNIX_SOCKET=
WEBHOOK_FORWARD_GROUP=webhook-forwarders
WEBHOOK_FORWARD_CONSUMER=worker-1
WEBHOOK_FORWARD_TIMEOUT=20
WEBHOOK_FORWARD_MAX_RETRIES=20
WEBHOOK_FORWARD_RETRY_DELAY=3
WEBHOOK_FORWARD_BATCH_SIZE=10
WEBHOOK_FORWARD_BLOCK_MS=5000
WEBHOOK_FORWARD_PENDING_IDLE_MS=60000
```

## Run Endpoint

Run the WSGI endpoint with Gunicorn:

```bash
gunicorn separator.webhook_buffer.wsgi:application \
  --bind unix:/run/webhook-buffer.sock \
  --workers 2 \
  --timeout 10
```

## Run Worker

Run the forwarding worker as a separate long-running process:

```bash
python3 -m separator.webhook_buffer.worker
```

The worker is not started through Gunicorn.

To forward directly to the main Gunicorn unix socket:

```bash
WEBHOOK_FORWARD_UNIX_SOCKET=/run/separator.sock \
python3 -m separator.webhook_buffer.worker
```

When `WEBHOOK_FORWARD_UNIX_SOCKET` is set, `WEBHOOK_FORWARD_URL` is ignored.

## Nginx Example

```nginx
upstream separator_main {
    server 10.9.0.6:8000;
}

upstream webhook_buffer {
    server 10.9.0.6:9000;
}

server {
    listen 80;
    server_name example.com;

    proxy_connect_timeout 1s;
    proxy_send_timeout 3s;
    proxy_read_timeout 3s;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location ^~ /api/bitrix/ {
        proxy_intercept_errors on;
        error_page 500 502 503 504 = @webhook_buffer;
        proxy_pass http://separator_main;
    }

    location = /api/waba/ {
        proxy_intercept_errors on;
        error_page 500 502 503 504 = @webhook_buffer;
        proxy_pass http://separator_main;
    }

    location @webhook_buffer {
        proxy_pass http://webhook_buffer;
    }

    location / {
        proxy_pass http://separator_main;
    }
}
```

## Redis Persistence

Use a local Redis instance with persistence enabled. Recommended minimum:

```conf
appendonly yes
appendfsync everysec
```

Without Redis persistence, queued webhooks can be lost during a server restart.

## Local Redis Systemd Example

Example `/etc/redis/buffer.conf`:

```conf
port 6381
bind 127.0.0.1
protected-mode yes

supervised no
dir /var/lib/redis-buffer
dbfilename dump.rdb

appendonly yes
appendfilename "appendonly.aof"
appendfsync everysec

save ""
```

Create the data directory:

```bash
sudo mkdir -p /var/lib/redis-buffer
sudo chown redis:redis /var/lib/redis-buffer
sudo chmod 750 /var/lib/redis-buffer
```

Example `/etc/systemd/system/redis-buffer.service`:

```ini
[Unit]
Description=Redis Buffer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/redis-server /etc/redis/buffer.conf
ExecStop=/usr/bin/redis-cli -p 6381 shutdown
Restart=always
RestartSec=3
User=redis
Group=redis
RuntimeDirectory=redis-buffer
RuntimeDirectoryMode=0755
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable redis-buffer
sudo systemctl start redis-buffer
redis-cli -p 6381 ping
```

Use this Redis instance for the buffer:

```env
WEBHOOK_BUFFER_REDIS_URL=redis://127.0.0.1:6381/0
```

## Notes

- The WABA raw request body is preserved, so downstream signature verification can still use the original payload.
- The original `Host` header is preserved because some webhook handlers depend on it.
- The `Content-Length` header is removed before forwarding because it must match the replayed request.
- Webhook processing should be idempotent because retries can deliver the same webhook more than once.
- Successfully delivered messages are acknowledged and deleted from the Redis Stream.

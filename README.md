[Русский](README_ru.md)

## Separator.biz: Bitrix24 Integration Hub 

### Description

One separator installation allows you to create and manage an unlimited number of local and mass-distributed Bitrix24 applications with OAuth 2.0 authorization.

## Video Instructions on YouTube

https://www.youtube.com/playlist?list=PLeniNJl73vVmmsG1XzTlimbZJf969LIpS

## Installation

### Docker (Recommended)

1.  Clone the repository:
    ```bash
    git clone https://github.com/estvita/separator
    cd separator
    ```

2.  Configure environment:
    ```bash
    # Automatic setup (generates keys and configs)
    make setup-evolution
    
    # OR Manual setup
    cp docs/example/env.example .env
    nano .env
    ```
    *Ensure `ASTERX_SERVER=True` is set if you need the AsterX service.*

3.  Start with Docker Compose:
    ```bash
    # Start all services (Separator + Evolution)
    docker compose -f docker-compose.yml -f docker-compose.evolution.yml up -d --build
    
    # OR Start only Separator
    docker compose up -d --build
    ```

4.  Create a superuser:
    ```bash
    docker compose run --rm web python manage.py createsuperuser
    ```

[Migration Guide from Systemd to Docker](docs/docker_migration.md)

### Manual Installation

+ Python 3.12
+ PostgreSQL 16
+ [Redis Stack](https://redis.io/docs/latest/operate/oss_and_stack/install/archive/install-stack/)

```
git clone https://github.com/estvita/separator
cd separator


python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/production.txt

cp docs/example/env.example .env
nano .env 

replace DJANGO_ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS with your values 
Replace the value of DATABASE_URL with your own (the psql database must be created beforehand)

python manage.py migrate 
python manage.py collectstatic 
python manage.py createsuperuser

python manage.py runserver 0.0.0.0:8000 (for testing and debugging)


sudo cp docs/example/celery_worker.service /etc/systemd/system/celery_worker.service
sudo cp docs/example/celery_beat.service /etc/systemd/system/celery_beat.service

sudo systemctl daemon-reload
sudo systemctl enable celery_worker.service
sudo systemctl enable celery_beat.service
sudo systemctl start celery_worker.service
sudo systemctl start celery_beat.service

```

The default path to access the admin panel is /admin. To set your own path, change the DJANGO_ADMIN_URL variable in the .env file.

## Celery Configuration (Docker only)

When running in Docker, you can configure the concurrency (number of worker processes) for each Celery queue using environment variables in your `.env` file. This is useful for optimizing resource usage based on your server's capabilities.

Default value is 3 for all queues.

To disable a specific worker, set its concurrency to 0.

Available variables:
- `CELERY_BITRIX_CONCURRENCY`: Concurrency for Bitrix24 tasks.
- `CELERY_OLX_CONCURRENCY`: Concurrency for OLX tasks.
- `CELERY_WAWEB_CONCURRENCY`: Concurrency for WhatsApp Web tasks.
- `CELERY_WABA_CONCURRENCY`: Concurrency for WhatsApp Business API tasks.
- `CELERY_BITBOT_CONCURRENCY`: Concurrency for BitBot tasks.
- `CELERY_DEFAULT_CONCURRENCY`: Concurrency for default tasks.

Example `.env` configuration:
```bash
CELERY_BITRIX_CONCURRENCY=5
CELERY_WAWEB_CONCURRENCY=2
```

## Database
The [DJ-Database-URL](https://github.com/jazzband/dj-database-url?tab=readme-ov-file#url-schema) module allows connecting various databases. See the documentation via the link.

## Update

```
cd separator
git pull
source .venv/bin/activate
python manage.py migrate
deactivate
sudo systemctl restart separator
```


## Proxy Server
+ You can view the process of setting up Nginx and Gunicorn [here](https://www.digitalocean.com/community/tutorials/how-to-set-up-django-with-postgres-nginx-and-gunicorn-on-ubuntu)
+ Example configuration files are available in the [documentation](/docs/example)



## Integrations

+ [Bitrix24 CRM](/docs/bitrix.md)
+ [PBX based on Asterisk](/docs/asterx.md)
+ [WhatsApp - WABA](/docs/waba.md)
+ [WhatsApp - WEB](/docs/waweb.md)
+ [OLX](/docs/olx.md)
+ [BitBot](/docs/bitbot.md) - Dify, Typebot, Langflow


## User Service Pages
+ /portals/ - Bitrix24
+ /asterx/ - IP PBX Asterisk
+ /olx/accounts/ - OLX
+ /waba/ - waba
+ /waweb/ - whatsapp web
+ /bitbot/ - chat-bot connector
[Русский](README_ru.md)

## Separator.biz: Bitrix24 Integration Hub 

### Description

One separator installation allows you to create and manage an unlimited number of local and mass-distributed Bitrix24 applications with OAuth 2.0 authorization.

## Video Instructions on YouTube

https://www.youtube.com/playlist?list=PLeniNJl73vVmmsG1XzTlimbZJf969LIpS

## Installation

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


+ [Chatwoot](/docs/chatwoot.md)


## User Service Pages
+ /portals/ - Bitrix24
+ /asterx/ - IP PBX Asterisk
+ /olx/accounts/ - OLX
+ /waba/ - waba
+ /waweb/ - whatsapp web

+ /dify/ - dify bots

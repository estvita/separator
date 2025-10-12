**## Separator.biz: Bitrix24 Integration Hub**

**### Описание**

Одна инсталляция separator позволяет создавать и обслуживать неограниченное количество локальных и тиражных приложений Битрикс24 с OAuth 2.0 авторизацией.

**## Видеоинструкции на Youtube**

https://www.youtube.com/playlist?list=PLeniNJl73vVmmsG1XzTlimbZJf969LIpS

**## Установка**

+ Python 3.12
+ PostgreSQL 16
+ Redis Stack

```

cd /opt
git clone https://github.com/estvita/separator
cd separator


python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/production.txt

cp docs/example/env_example .env
nano .env
заменить ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS на свои значения
заменить значение DATABASE_URL на свое значение (база psql должна быть предварительно создана)

python manage.py migrate
python manage.py collectstatic
python manage.py createsuperuser

python manage.py runserver 0.0.0.0:8000   # для тестирования и отладки

sudo cp docs/example/celery_worker.service /etc/systemd/system/celery_worker.service
sudo cp docs/example/celery_beat.service /etc/systemd/system/celery_beat.service

sudo systemctl daemon-reload
sudo systemctl enable celery_worker.service
sudo systemctl enable celery_beat.service
sudo systemctl start celery_worker.service
sudo systemctl start celery_beat.service
```
Путь по умолчанию для входа в админку: /admin. Чтобы задать свой путь — измените значение переменной DJANGO_ADMIN_URL в .env

**## База данных**

Модуль [DJ-Database-URL](https://github.com/jazzband/dj-database-url?tab=readme-ov-file#url-schema) позволяет подключать различные базы. См. документацию по ссылке.


**## Обновление**
```
cd /opt/separator
git  pull
source .venv/bin/activate
python manage.py migrate
deactivate
sudo systemctl restart separator
```

**## Прокси-сервер**

+ Процесс настройки Nginx и Gunicorn можно посмотреть [здесь](https://www.digitalocean.com/community/tutorials/how-to-set-up-django-with-postgres-nginx-and-gunicorn-on-ubuntu)
+ Примеры файлов конфигураций есть в [документации](/docs/example)


**## Подключение**

+ [CRM Битрикс24](/docs/bitrix.ru.md)
+ [АТС на базе Asterisk](/docs/asterx.ru.md)
+ [(WhatsApp) WABA](/docs/waba.ru.md)
+ [WhatsApp - WEB](/docs/waweb.ru.md)
+ [OLX](/docs/olx.md)


+ [Chatwoot](/docs/chatwoot.md)

**## Адреса пользовательских интерфейсов**

+ /portals/ — Bitrix24
+ /asterx/ - IP АТС Asterisk
+ /olx/accounts/ — OLX
+ /waba/ — WABA
+ /waweb/ — WhatsApp Web

+ /dify/ — Dify Bots

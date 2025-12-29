**## Separator.biz: Bitrix24 Integration Hub**

**### Описание**

Одна инсталляция separator позволяет создавать и обслуживать неограниченное количество локальных и тиражных приложений Битрикс24 с OAuth 2.0 авторизацией.

**## Видеоинструкции на Youtube**

https://www.youtube.com/playlist?list=PLeniNJl73vVmmsG1XzTlimbZJf969LIpS

**## Установка**

### Docker (Рекомендуется)

1.  Клонируйте репозиторий:
    ```bash
    git clone https://github.com/estvita/separator
    cd separator
    ```

2.  Настройте окружение:
    ```bash
    cp docs/example/env.example .env
    nano .env
    ```
    *Убедитесь, что `ASTERX_SERVER=True` установлен, если вам нужен сервис AsterX.*
    *Установите `SALT_KEY` в надежную случайную строку для шифрования полей базы данных. Вы можете сгенерировать её командой `openssl rand -base64 32`.*

3.  Запустите с помощью Docker Compose:
    ```bash
    docker compose up -d --build
    ```

4.  Создайте суперпользователя:
    ```bash
    docker compose run --rm web python manage.py createsuperuser
    ```

[Руководство по миграции с Systemd на Docker](docs/docker_migration.ru.md)

### Шифрование данных

Чувствительные данные (токены, секреты, пароли) в базе данных шифруются с использованием `SALT_KEY`.
Если вы мигрируете существующую инсталляцию на использование шифрования, пожалуйста, следуйте [Руководству по миграции шифрования](docs/encryption_migration.ru.md).

### Ручная установка

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
# Установите SALT_KEY для шифрования
# заменить DJANGO_ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS на свои значения
# заменить значение DATABASE_URL на свое значение (база psql должна быть предварительно создана)

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

## Настройка Celery (только для Docker)

При запуске в Docker вы можете настроить конкурентность (количество рабочих процессов) для каждой очереди Celery, используя переменные окружения в файле `.env`. Это полезно для оптимизации использования ресурсов в зависимости от возможностей вашего сервера.

Значение по умолчанию — 3 для всех очередей.

Чтобы отключить конкретного воркера, установите его конкурентность в 0.

Доступные переменные:
- `CELERY_BITRIX_CONCURRENCY`: Конкурентность для задач Bitrix24.
- `CELERY_OLX_CONCURRENCY`: Конкурентность для задач OLX.
- `CELERY_WAWEB_CONCURRENCY`: Конкурентность для задач WhatsApp Web.
- `CELERY_WABA_CONCURRENCY`: Конкурентность для задач WhatsApp Business API.
- `CELERY_BITBOT_CONCURRENCY`: Конкурентность для задач BitBot.
- `CELERY_DEFAULT_CONCURRENCY`: Конкурентность для задач по умолчанию.

Пример настройки в `.env`:
```bash
CELERY_BITRIX_CONCURRENCY=5
CELERY_WAWEB_CONCURRENCY=2
```

**## База данных**

Модуль [DJ-Database-URL](https://github.com/jazzband/dj-database-url?tab=readme-ov-file#url-schema) позволяет подключать различные базы. См. документацию по ссылке.


**## Обновление**
```
cd separator
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

**## Адреса пользовательских интерфейсов**

+ /portals/ — Bitrix24
+ /asterx/ - IP АТС Asterisk
+ /olx/accounts/ — OLX
+ /waba/ — WABA
+ /waweb/ — WhatsApp Web

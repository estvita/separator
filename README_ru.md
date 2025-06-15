**## Thoth: Bitrix24 Integration Hub**

**### Описание**

Одна инсталляция Thoth позволяет создавать и обслуживать неограниченное количество локальных и тиражных приложений Битрикс24 с OAuth 2.0 авторизацией.

**## Видеоинструкции на Youtube**

https://www.youtube.com/playlist?list=PLeniNJl73vVmmsG1XzTlimbZJf969LIpS

**## Установка**

+ Python 3.12
+ PostgreSQL 16
+ Redis

```
cd /opt
git clone https://github.com/estvita/thoth
cd thoth

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
```
Путь по умолчанию для входа в админку: /admin. Чтобы задать свой путь — измените значение переменной DJANGO_ADMIN_URL в .env

**## База данных**

Модуль [DJ-Database-URL](https://github.com/jazzband/dj-database-url?tab=readme-ov-file#url-schema) позволяет подключать различные базы. См. документацию по ссылке.

**## Обновление**
```
cd /opt/thoth
source .venv/bin/activate
git pull
python manage.py migrate
systemctl restart thoth
```

**## Прокси-сервер**

+ Процесс настройки Nginx и Gunicorn можно посмотреть [здесь](https://www.digitalocean.com/community/tutorials/how-to-set-up-django-with-postgres-nginx-and-gunicorn-on-ubuntu)
+ Примеры файлов конфигураций есть в [документации](docs/example)

**## Логирование**

При необходимости можно включить подробные логи в консоль. Для этого в файле .env укажите уровень логирования LOG_LEVEL=DEBUG, перезапустите thoth и введите команду

```
journalctl -u thoth -f
```

**## Подключение**

+ [Битрикс](docs/bitrix.ru.md)
+ [(WhatsApp) WABA](docs/waba.md)
+ [WhatsApp - WEB](docs/waweb.md)
+ [OLX](docs/olx.md)
+ [Chatwoot](docs/chatwoot.md)
+ [OpenAI chat-bot](docs/openai_bot.md)
+ [OpenAI voice-bot](docs/openai_voice.md)

**## Адреса пользовательских интерфейсов**

+ /portals/ — Bitrix24
+ /olx/accounts/ — OLX
+ /waba/ — WABA
+ /waweb/ — WhatsApp Web
+ /bots/ — OpenAI Assistants
+ /voices/ — OpenAI Voice
+ /dify/ — Dify Bots
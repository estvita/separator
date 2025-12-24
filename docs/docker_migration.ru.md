# Миграция на Docker

Это руководство описывает процесс миграции существующего production-развертывания (Systemd/Gunicorn) на архитектуру с использованием Docker.

## Предварительные требования

1.  **Бэкап**: Убедитесь, что у вас есть полный бэкап базы данных PostgreSQL и медиа-файлов.
2.  **Docker**: Убедитесь, что Docker и Docker Compose установлены на сервере.
3.  **Код**: Получите последнюю версию репозитория (`git pull`).

## Пошаговая миграция

### 1. Бэкап данных

Создайте дамп текущей базы данных:
```bash
pg_dump -U separator separator > separator_backup.sql
```
*Замените `separator` на вашего пользователя и имя БД, если они отличаются.*

Убедитесь, что у вас есть копия медиа-файлов (обычно в `separator/media`).

### 2. Остановка старых сервисов

Остановите и отключите systemd сервисы, чтобы избежать конфликтов:

```bash
sudo systemctl stop separator-web separator-worker separator-beat asterx
sudo systemctl disable separator-web separator-worker separator-beat asterx
```

### 3. Настройка окружения

Обновите файл `.env`. Добавьте следующую переменную для включения отдельного контейнера AsterX:

```bash
ASTERX_SERVER=True
```

### 4. Импорт базы данных в Docker

**Важно:** Если вы уже запускали контейнеры ранее, очистите старые данные (включая тома БД), чтобы избежать конфликтов:
```bash
docker compose down -v --remove-orphans
```

Запустите контейнер базы данных:
```bash
docker compose up -d db
```

Скопируйте дамп внутрь контейнера:
```bash
docker cp separator_backup.sql separator-db-1:/tmp/backup.sql
```

Восстановите базу данных.

**Вариант А (для обычного SQL-дампа):**
```bash
docker exec -i separator-db-1 psql -U separator -d separator -f /tmp/backup.sql
```

**Вариант Б (для бинарного дампа / custom format):**
```bash
docker exec -i separator-db-1 pg_restore -U separator -d separator --clean --if-exists /tmp/backup.sql
```

### 5. Восстановление медиа-файлов

Убедитесь, что ваши медиа-файлы находятся в папке `separator/media/` внутри директории проекта. Docker монтирует директорию проекта в `/app`, поэтому файлы в `separator/media` будут доступны контейнерам.

### 6. Запуск контейнеров

Соберите и запустите приложение:

```bash
docker compose up -d --build
```

Проверьте логи, чтобы убедиться, что все работает:
```bash
docker compose logs -f
```

### 7. Обновление конфигурации Nginx

Обновите конфигурацию Nginx на хосте, чтобы проксировать запросы на порты Docker (8000 для Web, 9000 для AsterX).

Пример фрагмента конфигурации:

```nginx
upstream django_server {
    server 127.0.0.1:8000;
}

upstream asterx_server {
    server 127.0.0.1:9000;
}

server {
    # ... существующий конфиг ...

    location / {
        proxy_pass http://django_server;
        # ... заголовки прокси ...
    }

    location /ws/ {
        proxy_pass http://asterx_server;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    
    # Раздача статики и медиа напрямую, если пути совпадают
    location /static/ {
        alias /path/to/project/staticfiles/;
    }

    location /media/ {
        alias /path/to/project/separator/media/;
    }
}
```

Перезагрузите Nginx:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

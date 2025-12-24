# Migration to Docker

This guide describes the process of migrating an existing production deployment (Systemd/Gunicorn) to the Docker-based setup.

## Prerequisites

1.  **Backup**: Ensure you have a full backup of your PostgreSQL database and media files.
2.  **Docker**: Ensure Docker and Docker Compose are installed on the server.
3.  **Code**: Pull the latest version of the repository.

## Step-by-Step Migration

### 1. Backup Data

Create a dump of your current database:
```bash
pg_dump -U separator separator > separator_backup.sql
```
*Replace `separator` with your actual database user and name if different.*

Ensure you have a copy of your media files (usually in `separator/media`).

### 2. Stop Old Services

Stop and disable the systemd services to prevent conflicts:

```bash
sudo systemctl stop separator-web separator-worker separator-beat asterx
sudo systemctl disable separator-web separator-worker separator-beat asterx
```

### 3. Configure Environment

Update your `.env` file. Add the following variable to enable the separate AsterX container:

```bash
ASTERX_SERVER=True
```

### 4. Import Database to Docker

Start the database container:
```bash
docker compose up -d db
```

Copy the backup into the container:
```bash
docker cp separator_backup.sql separator-db-1:/tmp/backup.sql
```

Restore the database:
```bash
docker exec -i separator-db-1 psql -U separator -d separator -f /tmp/backup.sql
```

### 5. Restore Media Files

Ensure your media files are located in `separator/media/` within the project directory. Docker mounts the project directory to `/app`, so files in `separator/media` will be accessible.

### 6. Start Containers

Build and start the application:

```bash
docker compose up -d --build
```

Check logs to ensure everything is running:
```bash
docker compose logs -f
```

### 7. Update Nginx Configuration

Update your host Nginx configuration to proxy requests to the Docker ports (8000 for Web, 9000 for AsterX).

Example configuration snippet:

```nginx
upstream django_server {
    server 127.0.0.1:8000;
}

upstream asterx_server {
    server 127.0.0.1:9000;
}

server {
    # ... existing config ...

    location / {
        proxy_pass http://django_server;
        # ... proxy headers ...
    }

    location /ws/ {
        proxy_pass http://asterx_server;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    
    # Serve static and media files directly if mapped to host
    location /static/ {
        alias /path/to/project/staticfiles/;
    }

    location /media/ {
        alias /path/to/project/separator/media/;
    }
}
```

Reload Nginx:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

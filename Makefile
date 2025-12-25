.PHONY: up build down vendor vendor-build

# Запуск Production версии (без пересборки)
up:
	docker compose up

# Запуск Production версии с пересборкой (нужно при изменении requirements или Dockerfile)
build:
	docker compose up --build

# Остановка всех контейнеров
down:
	docker compose down

# Запуск Vendor версии (без пересборки)
vendor:
	docker compose -f docker-compose.yml -f docker-compose.vendor.yml up

# Запуск Vendor версии с пересборкой
vendor-build:
	docker compose -f docker-compose.yml -f docker-compose.vendor.yml up --build

# Создание миграций
makemigrations:
	docker compose run --rm web python manage.py makemigrations

# Применение миграций
migrate:
	docker compose run --rm web python manage.py migrate

# Создание суперпользователя
superuser:
	docker compose run --rm web python manage.py createsuperuser

# Настройка Evolution (генерация ключей и конфигов)
setup-evolution:
	python scripts/init_evolution.py

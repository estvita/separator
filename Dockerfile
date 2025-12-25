
# Use Python 3.12 slim image
FROM python:3.12-slim-bullseye

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # pip:
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    # poetry:
    POETRY_VERSION=1.7.1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR='/var/cache/pypoetry' \
    POETRY_HOME='/usr/local'

# Install system dependencies
RUN apt-get update \
  && apt-get install --no-install-recommends -y \
    bash \
    build-essential \
    curl \
    gettext \
    git \
    libmagic1 \
    libpq-dev \
    wget \
  # Cleaning cache:
  && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Install Python dependencies
COPY ./requirements /app/requirements

ARG DJANGO_SETTINGS_MODULE=config.settings.production
ARG ASTERX_SERVER=False

RUN if [ "$DJANGO_SETTINGS_MODULE" = "config.settings.vendor" ]; then \
        pip install -r requirements/vendor.txt; \
    else \
        pip install -r requirements/production.txt; \
    fi

RUN if [ "$ASTERX_SERVER" = "True" ] || [ "$ASTERX_SERVER" = "true" ]; then \
        pip install -r requirements/asterx.txt; \
    fi

# Install flower explicitly if not in requirements (it usually isn't in production.txt)
RUN pip install flower

# Copy project
COPY . /app

# Create entrypoint script
COPY ./compose/production/django/entrypoint /entrypoint
RUN sed -i 's/\r$//g' /entrypoint
RUN chmod +x /entrypoint

COPY ./compose/production/django/start /start
RUN sed -i 's/\r$//g' /start
RUN chmod +x /start

COPY ./compose/production/django/start-asgi /start-asgi
RUN sed -i 's/\r$//g' /start-asgi
RUN chmod +x /start-asgi

COPY ./compose/production/django/celery/worker/start /start-celeryworker
RUN sed -i 's/\r$//g' /start-celeryworker
RUN chmod +x /start-celeryworker

COPY ./compose/production/django/celery/beat/start /start-celerybeat
RUN sed -i 's/\r$//g' /start-celerybeat
RUN chmod +x /start-celerybeat

COPY ./compose/production/django/celery/flower/start /start-flower
RUN sed -i 's/\r$//g' /start-flower
RUN chmod +x /start-flower

ENTRYPOINT ["/entrypoint"]

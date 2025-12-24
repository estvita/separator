# Separator.biz: Bitrix24 Integration Hub - AI Agent Instructions

## Architecture Overview
This is a Django-based integration hub for Bitrix24 applications with OAuth 2.0. Core components:
- **bitrix app**: Manages Bitrix24 portals, apps, and OAuth flows
- **Connector model**: Links apps to services (OLX, WhatsApp Web, WhatsApp Cloud)
- **Service-specific apps**: `waba`, `waweb`, `olx`, `asterx`, `freepbx`, `bitbot` handle integrations
- **Celery queues**: `bitrix`, `olx`, `waweb`, `waba`, `bitbot`, `default` for async processing
- **Docker Services**: Dedicated containers for `web`, `db`, `redis`, `beat`, `flower`, `asterx` (ASGI), and separate workers for each queue (`worker_bitrix`, `worker_olx`, etc.)
- **ASGI setup**: Daphne + Channels for AsterX WebSocket connections (running in `asterx` container)

## Key Patterns
- **App-Connector Relationship**: Apps have many-to-many with Connectors; events trigger via `ONIMCONNECTORMESSAGEADD` etc.
- **UUID Codes**: Use `generate_uuid()` prefixing with "gulin_" for unique identifiers
- **Event Handling**: Webhooks at `/api/{service}/` route to service-specific handlers
- **User Linking**: `link_objects()` assigns ownership of portals/instances to authenticated users
- **Validation**: SVG-only icons for connectors via `validate_svg()`
- **Async Emails**: System emails (registration, password reset) are handled asynchronously via `send_allauth_email_task` in the `default` queue

## Development Workflow
- **Docker Run**: `docker compose up -d` (starts all services including workers)
- **Restart Service**: `docker compose restart <service_name>` (e.g., `worker_default`)
- **Run Server (Manual)**: `python manage.py runserver 0.0.0.0:8000`
- **Celery Worker (Manual)**: `celery -A config.celery_app worker -l info -c 3 -Q bitrix,olx,waweb,waba,bitbot,default`
- **Celery Beat**: `celery -A config.celery_app beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler`
- **Migrations**: Always run after pulling updates
- **Testing**: pytest with factories in `conftest.py`; fixtures for media storage

## Integration Points
- **Bitrix24 OAuth**: Apps created in B24 with handler paths like `/api/bitrix/` or `/app-settings/`
- **External APIs**: Facebook Graph API for WABA, Typebot/Dify for BitBot, OpenAI-compatible APIs (e.g., OpenAI, Grok), OLX APIs
- **Webhooks**: REST endpoints for event processing; CSRF-exempt for external calls
- **Phone Validation**: Optional via `CHECK_PHONE_NUMBER` env var

## Conventions
- **Settings Structure**: Base settings in `config/settings/`, env-based config
- **App Registration**: Add to `LOCAL_APPS` in base.py for auto-discovery
- **Task Discovery**: Celery auto-discovers tasks from app configs
- **Translations**: Russian primary (`ru-RU`), English secondary; locale files in `locale/`
- **Static/Media**: Whitenoise for static, standard Django media handling

## Debugging
- **Launch Configs**: VS Code debug for app, worker, beat, flower
- **Logs**: Check Celery queues for async failures
- **Portal Linking**: Use verification codes for secure portal association
- **Event Flow**: Trace from B24 events → API → tasks → service actions

## Common Pitfalls
- **Queue Names**: Match exactly in worker commands and task decorators
- **OAuth Scopes**: Ensure `crm`, `im`, `imconnector` permissions in B24 apps
- **Environment**: Always activate venv; use production settings for Celery
- **Migrations**: Run before collectstatic on updates</content>
<parameter name="filePath">/home/anton/code/separator/.github/copilot-instructions.md
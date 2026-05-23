from django.db import transaction

from separator.bitrix.models import AppInstance, Line

from .models import Phone


def get_waba_app_instance_for_line(line):
    if not line or not line.portal_id:
        raise ValueError("Bitrix line is required")

    app_instance = (
        AppInstance.objects.filter(
            portal=line.portal,
            app__connectors__service="waba",
        )
        .select_related("app", "portal")
        .distinct()
        .get()
    )
    return app_instance


def get_waba_context_for_phone(phone):
    if not phone or not phone.line_id:
        raise ValueError("WABA phone is not connected to Bitrix line")

    line = phone.line
    app_instance = get_waba_app_instance_for_line(line)
    connector = line.connector or app_instance.app.connectors.filter(service="waba").first()
    if not connector:
        raise ValueError("WABA connector is not found")

    return {
        "phone": phone,
        "line": line,
        "connector": connector,
        "app_instance": app_instance,
    }


def get_waba_phone_for_line(app_instance, line_id):
    return (
        Phone.objects.select_related("line", "line__connector", "line__portal", "waba", "waba__app")
        .get(
            line__portal=app_instance.portal,
            line__line_id=line_id,
            line__connector__service="waba",
        )
    )


def _line_name_for_phone(phone):
    return phone.phone or phone.phone_id or "WhatsApp"


def _connector_for_app_instance(app_instance):
    connector = app_instance.app.connectors.filter(service="waba").first()
    if not connector:
        raise ValueError("WABA connector is not found")
    return connector


def resolve_line_selector(line_selector, phone):
    from separator.bitrix import tasks as bitrix_tasks

    line_selector = str(line_selector or "")
    if not line_selector:
        raise ValueError("Bitrix line is required")

    if line_selector.startswith("create__"):
        instance_id = line_selector.split("__", 1)[1]
        app_instance = AppInstance.objects.select_related("app", "portal").get(id=instance_id)
        connector = _connector_for_app_instance(app_instance)
        line_name = _line_name_for_phone(phone)
        params = {}
        if connector.default_line_params and isinstance(connector.default_line_params, dict):
            params.update(connector.default_line_params)
        params["LINE_NAME"] = line_name
        params["ACTIVE"] = "Y"
        result = bitrix_tasks.call_api(app_instance.id, "imopenlines.config.add", {"PARAMS": params})
        new_line_id = (result or {}).get("result")
        if not new_line_id:
            raise ValueError(f"Bitrix line creation failed: {result}")
        line = Line.objects.create(
            line_id=new_line_id,
            portal=app_instance.portal,
            connector=connector,
            name=line_name,
        )
        return line, app_instance, connector

    line = Line.objects.select_related("portal", "connector").get(id=line_selector)
    app_instance = get_waba_app_instance_for_line(line)
    connector = _connector_for_app_instance(app_instance)
    return line, app_instance, connector


def relink_waba_phone(phone, line_selector):
    from separator.bitrix import tasks as bitrix_tasks

    with transaction.atomic():
        phone = Phone.objects.select_for_update().get(id=phone.id)
        old_line = (
            Line.objects.select_related("connector", "portal").filter(id=phone.line_id).first()
            if phone.line_id
            else None
        )
        line, app_instance, connector = resolve_line_selector(line_selector, phone)

        if line.connector_id and line.connector_id != connector.id:
            if line.olx_users.exists() or line.wawebs.exists():
                raise ValueError("Line is connected to another service")
        if line.connector_id != connector.id:
            line.connector = connector
            line.save(update_fields=["connector"])

        if old_line and old_line.id != line.id:
            old_app_instance = get_waba_app_instance_for_line(old_line)
            old_connector = old_line.connector or _connector_for_app_instance(old_app_instance)
            bitrix_tasks.call_api(
                old_app_instance.id,
                "imconnector.activate",
                {"CONNECTOR": old_connector.code, "LINE": old_line.line_id, "ACTIVE": 0},
            )

        Phone.objects.filter(line=line).exclude(id=phone.id).update(line=None)

        bitrix_tasks.call_api(
            app_instance.id,
            "imconnector.activate",
            {"CONNECTOR": connector.code, "LINE": line.line_id, "ACTIVE": 1},
        )

        phone.line = line
        phone.save(update_fields=["line"])

    bitrix_tasks.messageservice_add.delay(app_instance.id, phone.id, "waba")
    return line


def sync_waba_sms_sender(phone):
    context = get_waba_context_for_phone(phone)
    from separator.bitrix import tasks as bitrix_tasks

    bitrix_tasks.messageservice_add.delay(context["app_instance"].id, phone.id, "waba")


def line_status_label(line):
    return f"{line.name} ({line.line_id})"

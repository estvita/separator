from django.contrib import admin
from django.contrib import messages

from thoth.bitrix.crest import call_method
from thoth.bitrix.models import Line
from thoth.bitrix.utils import messageservice_add

from .models import App, Waba, Phone, Template

@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("client_id", "verify_token")

@admin.register(Waba)
class WabaAdmin(admin.ModelAdmin):
    list_display = ("waba_id", "owner")
    list_per_page = 30

@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "lang", "owner", "waba", "status")
    list_per_page = 30


@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    list_display = ("phone_id", "phone", "owner", "waba", "line", "sms_service")
    search_fields = ("phone", "phone_id")
    list_per_page = 30

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        # создание открытой линии
        if not obj.line and obj.app_instance:
            line_data = {
                "PARAMS": {
                    "LINE_NAME": obj.phone,
                },
            }

            create_line = call_method(
                obj.app_instance, "imopenlines.config.add", line_data
            )

            # активация открытой линии
            if "result" in create_line:
                line = Line.objects.create(
                    line_id=create_line["result"],
                    app_instance=obj.app_instance,
                )
                obj.line = line
                obj.save()

                payload = {
                    "CONNECTOR": "thoth_waba",
                    "LINE": line.line_id,
                    "ACTIVE": 1,
                }

                call_method(obj.app_instance, "imconnector.activate", payload)


        # Регистрация SMS-провайдера
        if obj.sms_service:
            if not obj.line or not obj.app_instance:
                obj.sms_service = False
                obj.save()
                messages.error(request, 'phone not have line (not connected to bitrix24)')
                return
            # Проверка наличия объекта auth_token
            owner = obj.line.app_instance.app.owner
            if not hasattr(owner, 'auth_token'):
                obj.sms_service = False
                obj.save()
                messages.error(request, f"API key not found for user {owner}. Operation aborted.")
                return

            api_key = owner.auth_token.key
            resp = messageservice_add(obj.app_instance, obj.phone, obj.line.line_id, api_key, 'waba')
            if 'error' in resp:
                messages.error(request, resp)
            else:
                messages.info(request, resp)

        elif not obj.sms_service:
            if not obj.line or not obj.app_instance:
                messages.error(request, 'phone not have line (not connected to bitrix24)')
                return
            phone = ''.join(filter(str.isalnum, obj.phone))
            resp = call_method(
                obj.app_instance,
                "messageservice.sender.delete",
                {"CODE": f"THOTH_{phone}_{obj.line.line_id}"},
            )
            if 'error' in resp:
                messages.error(request, resp)
            else:
                messages.info(request, resp)

        obj.save()

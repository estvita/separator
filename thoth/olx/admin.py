from django.contrib import admin

from .models import OlxApp
from .models import OlxUser


@admin.register(OlxApp)
class OlxAppAdmin(admin.ModelAdmin):
    list_display = ("name", "client_domain", "owner", "client_id")
    readonly_fields = ("authorization_link",)

@admin.register(OlxUser)
class OlxUserAdmin(admin.ModelAdmin):
    list_display = (
        "olx_id",
        "owner",
        "date_end",
        "email",
        "line",
    )
    search_fields = ("olx_id", "email", "name", "phone")
    readonly_fields = (
        # "access_token",
        # "refresh_token",
        "olx_id",
        "email",
        "name",
        "phone",
        "olxapp",
        # "line",
    )
    list_per_page = 30

    # def save_model(self, request, obj, form, change):
    #     super().save_model(request, obj, form, change)

    #     # Проверяем, есть ли привязка к объекту Битрикс и отсутствует ли линия
    #     if obj.bitrix and not obj.line:
    #         # Создание открытой линии в Битрикс
    #         line_data = {
    #             "PARAMS": {
    #                 "LINE_NAME": f"THOTH_OLX_{obj.olx_id}",
    #             },
    #         }

    #         create_line = call_method(obj.bitrix, "imopenlines.config.add", line_data)

    #         # Активация открытой линии
    #         if "result" in create_line:
    #             # Создаем запись в модели Line и связываем её с текущим объектом OlxUser
    #             line = Line.objects.create(
    #                 line_id=create_line["result"],
    #                 portal=obj.bitrix,
    #                 content_object=obj,
    #             )
    #             obj.line = line
    #             obj.save()

    #             payload = {
    #                 "CONNECTOR": "thoth_olx",
    #                 "LINE": line.line_id,
    #                 "ACTIVE": 1,
    #             }

    #             call_method(obj.bitrix, "imconnector.activate", payload)

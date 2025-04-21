from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from thoth.bitrix.crest import call_method
from thoth.bitrix.models import AppInstance, Line

from .models import OlxApp, OlxUser


@login_required
def olx_accounts(request):
    # Получаем учетные записи OLX, связанные с текущим пользователем
    olx_accounts = OlxUser.objects.filter(owner=request.user)

    # Получаем список доступных OLX приложений
    olx_apps = OlxApp.objects.all()

    # Получаем список AppInstance, принадлежащих текущему пользователю и связанных с приложением "OLX"
    app_instances = AppInstance.objects.filter(
        app__name="olx",
        portal__owner=request.user,
    )

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "link":
            olx_user_id = request.POST.get("olx_user_id")
            app_instance_id = request.POST.get("app_instance")

            try:
                olx_user = OlxUser.objects.get(id=olx_user_id, owner=request.user)
                app_instance = AppInstance.objects.get(id=app_instance_id)

                # Проверка на существование app_instance
                if olx_user.line:
                    # 1. Если AppInstance совпадает, ничего не делаем
                    if olx_user.line.app_instance == app_instance:
                        return redirect("olx-accounts")
                    
                    # 3. Если AppInstance отличается, удаляем старую линию, создаём новую
                    else:
                        old_line = olx_user.line
                        # Удаление старой линии
                        olx_user.line = None
                        call_method(
                            old_line.app_instance,
                            "imopenlines.config.delete",
                            {"CONFIG_ID": old_line.line_id},
                        )
                
                # 2. Если AppInstance отсутствует, создаём линию
                line_data = {
                    "PARAMS": {
                        "LINE_NAME": olx_user.olx_id,
                    },
                }

                create_line = call_method(
                    app_instance, "imopenlines.config.add", line_data
                )

                if "result" in create_line:
                    # Линия успешно создана
                    line = Line.objects.create(
                        line_id=create_line["result"],
                        app_instance=app_instance,
                    )

                    olx_user.line = line
                    olx_user.line.app_instance = app_instance
                    olx_user.save()

                    payload = {
                        "CONNECTOR": "thoth_olx",
                        "LINE": line.line_id,
                        "ACTIVE": 1,
                    }

                    activate_resp = call_method(app_instance, "imconnector.activate", payload)

                    if activate_resp.get("error"):
                        print("Error activating connector:", activate_resp)

            except OlxUser.DoesNotExist:
                print("OlxUser does not exist:", olx_user_id)
            except AppInstance.DoesNotExist:
                print("AppInstance does not exist:", app_instance_id)
            except Exception as e:
                print("Unexpected error:", str(e))

        elif action == "connect":
            olx_app_id = request.POST.get("olx_app")
            olx_app = OlxApp.objects.get(id=olx_app_id)
            return redirect(olx_app.authorization_link)

        return redirect("olx-accounts")

    return render(
        request,
        "olx/accounts.html",
        {
            "olx_accounts": olx_accounts,
            "olx_apps": olx_apps,
            "app_instances": app_instances,
        },
    )

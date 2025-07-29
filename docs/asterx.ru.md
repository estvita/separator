# Интеграция CRM Битрикс24 с IP АТС на базе Asterisk

Работа коннектора [AsterX](https://github.com/estvita/AsterX) основана на взаимодействии с АТС через управляющий интерфейс AMI (Asterisk Manager Interface), соответственно, возможна интеграция Битрикс24 с любой АТС, работающей на Asterisk:

Программные АТС:
+ FreePBX (самый распространенный и с GUI)
+ Issabel (Elastix-Style)
+ VitalPBX
+ Asterisk в "чистом" виде (ручное конфигурирование)
+ и другие

Аппаратные решения на Asterisk с поддержкой AMI (зависит от моделей)
+ Yeastar
+ Grandstream
+ OpenVox 
+ и другие

Коонектор AsterX может работать самостоятельно или под управлением сервера thoth.

Алгоритм работы:
+ Подключение к AMI АТС
+ Подписка на события звонка
+ Регистрация звонка в Битрикс24 (telephony.externalcall.register)
+ Отображаение карточки клиента (telephony.externalcall.show). Возможные параметры: Не показывать, во время звонка, при ответе на звонок
+ Завершение звонка (telephony.externalcall.finish)
+ Прикрепление записи (telephony.externalCall.attachRecord)
+ Поддержка ClickToCall (OnExternalCallStart)


## Установка

На стороне Битрикс и thoth:
+ в .env thoth ASTERX_SERVER=True
+ Установить дополнительные пакеты из ![asterx.txt](/requirements/asterx.txt)
+ Создать локальное приложение Битрикс24, указать адреса https://example.com/app-install/ и https://example.com/app-settings/, задать права crm, user, disk. telephony, im. Нажать "Сохранить", сохранить client_id, client_secret

![asterx_b24](/docs/img/asterx_b24.png)
+ Создать приложение в интерфейсе thoth с событиями ONAPPUNINSTALL, ONEXTERNALCALLSTART. Отместить чекбокс "AsterX", Page url: /asterx/. Заполнить поля client_id, client_secret значениями из предыдущего пункта.
+ Нажать "Перейти к приложению" в Битрикс24
+ В открывшемся интерфейсе нажать кнопку "Добавить АТС"
![add_pbx](/docs/img/add_pbx.png)
+ Скопировать полученый PBX-ID: XXXXXX
+ Кликнуть по названию АТС и перейти в настройки, из выпадающего списка выбрать приложение AsterX на ... ваш портал битрикс, Сохранить

На стороне АТС 

Для программных АТС установку коннектора рекомендуется производить на самом сервере, для аппаратных платформ нужно использовать внешний сервер.
+ Создать AMI пользователя

Установка AsterX
```
git clone https://github.com/estvita/asterx.git
cd asterx
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/local_sql.txt
cp examples/cloud.ini config.ini
nano config.ini
```

```
[app]
control_server_http = https://example.com
control_server_ws = wss://example.com


[asterisk]
pbx_id = XXXXXX
host = localhost
port = 5038
username = AMI-username
secret = AMI-secret
```

Запустить в тестовом режиме 
```
python main.py
```

После авторизации коннектора на сервере thoth в базу коннектора будут переданы ключи и базовые настройки. 

Коннектор, в свою очередь, отправит на сервер список контекстов, из которых в настройках АТС в интерфейсе битрикс24 нужно выбрать внешние, внутренние и игнорируемые. 

Важно помнить, что во входящем или исходящем звонке участвует два контекста, соотвественно, в настройках должно быть как минимум по одному для внешней и внутренней линии. 

![asterx_settings](/docs/img/asterx_settings.png)

Для исключения звонков из статистики нужно выбрать "Исключить" 

Для сопоставления звонков между пользователми битрикс24 и АТС в настройках пользователей телефонии каждому необходимо присвоить номер, которым он пользуется в АТС и в качестве номера по умолчанию установить локальное приложение AsterX

После любых ихменений с настройками пользователей в списке АТС в иитерфейсе приложения необходмио нажать "Обновить" -  будет пересоздана локльаная база пользователей в коннекторе 
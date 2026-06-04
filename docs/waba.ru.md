## Подключение WhatsApp (WABA)

Видеоинструкция https://youtu.be/cSirpfq5rPQ
+ Рекомендуется получить [Постоянный маркер](https://developers.facebook.com/docs/whatsapp/business-management-api/get-started), иначе придется перевыпускать токен каждый день
+ Создайте приложение на [портале разработчиков](https://developers.facebook.com/apps/)
+ В панели подключите продукты Webhooks, WhatsApp
+ В админке separator - WABA - Add waba 
+ + name - имя вашего приложения 
+ + Access token - Постоянный или временный маркер
+ + Auth flow - способ авторизации на странице `/waba/`:
  + **Popup** - Embedded Signup v4 через Facebook JavaScript SDK. Браузер получает `waba_id`, `phone_number_id` и `code`; Separator добавляет конкретный выбранный номер.
  + **Manual** - старый redirect flow через Facebook OAuth и `/waba/callback/`.
  + **Hosted** - Hosted Embedded Signup от Meta. Результат обрабатывается через webhook `account_update / PARTNER_ADDED`.
+ + Business app onboarding - добавляет `extras.featureType=whatsapp_business_app_onboarding` для подключения WhatsApp Business App coexistence.
+ + После сохранения в списке WABA скопируйте Verify token для нужной учётки

+ На портале разработчиков - Quickstart > Configuration > 
+ + Callback URL - `https://example.com/api/waba/` или `https://example.com/api/waba/?app_id=YOUR_APP_ID`
  + Система определяет приложение по домену (`example.com`) или по параметру `app_id`, если он указан.
  + Безопасность обеспечивается проверкой подписи `X-Hub-Signature-256` с использованием вашего App Secret.
+ + Verify token - Verify token из предыдущего шага 
![alt text](img/verify.png)
+ В админке separator - waba - phones и добавляем номера (Phone - номер, Phone id - id из приложения фейсбук)
+ Выбрать объект waba, созданный ранее
+ Выбрать App instance (портал битрикс) к котрому привязать номер waba
+ Отметьте Чекбокс "Sms service", если хотите зарегистрировать этот номер в качестве [СМС провайдера](messageservice.md)  
+ File proxy - если включен, входящие медиафайлы проксируются в Bitrix по ссылке и не сохраняются во временные файлы Separator. Если выключен, Separator скачивает медиа и сохраняет временный файл.
+ если все пройдет успешно, то в контакт центре коннектор станет зеленым и кнему будет прикрпелена линия separator_ваш_номер
![ok](img/waba_ok.png)

## Партнерские приложения

Партнерские приложения настраиваются в **WABA - Partner apps**.

+ **Owner** - пользователь-интегратор, которому будут принадлежать подключенные WABA.
+ **App** - WABA App, через который выполняется подключение.
+ **Webhook URL** - callback URL партнера для подписки на webhook.
+ **Redirect URL** - адрес, куда Separator вернет клиента после подключения.

Для партнерских приложений всегда используется старый manual redirect flow, независимо от настройки **Auth flow** у App. Separator принимает callback, меняет `code` на токен, привязывает WABA к `partner_app.owner`, сохраняет `partner_app` в WABA и редиректит клиента на `redirect_url` партнера.

## Особенности работы WABA

+ Самое главное - первыми раз в сутки вы можете писать только используя заранее одобренный шаблон. Если линия (чат) уже создан можете отправить шаблон, используя конструкцию template-hello_world+en_US, где hello_world - назвение шаблона, en_US - язык шаблона. Так же можно отправить первое за сутки шаблонное сообщение через [SMS](messageservice.md)

### Интерактивные сообщения

Интерактивные сообщения настраиваются на странице `/waba/interactive/`.

Поддерживаемые типы:

+ Reply buttons
+ List
+ CTA URL
+ Call button
+ Call permission request

Каждое интерактивное сообщение получает shortcode:

```
interactive+INTERACTIVE_ID
```

Если в сообщении есть переменные, shortcode включает примеры значений:

```
interactive+INTERACTIVE_ID+name:value|name2:value2
```

Этот shortcode можно использовать из Bitrix Open Lines или SMS-подобного поля ввода, чтобы отправить заранее настроенный interactive payload.


# Активация SIP транка WhatsApp (приём звонков на сервер телефонии, например, Asterisk)

https://developers.facebook.com/docs/whatsapp/cloud-api/calling

+ После подключения номера перейдите в настройки (в админке или пользовательском интерфейсе) и установите значение "Звонки" и "SIP" - Включено
+ Укажите адрес (example.com) вашего сервера (должен быть настроен tls - трансопрт)
+ tls порт, по умолчанию 5061
+ Сохраните настройки. Если при активации приёма звонков с сервера Meta возникнет ошибка, её код будет отображаться в поле "Ошибка". 
Расшифровка кодов ошибок здесь - https://developers.facebook.com/docs/whatsapp/cloud-api/calling/troubleshooting/

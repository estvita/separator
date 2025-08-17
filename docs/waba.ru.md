## Подключение WhatsApp (WABA)

Видеоинструкция https://youtu.be/cSirpfq5rPQ
+ Рекомендуется получить [Постоянный маркер](https://developers.facebook.com/docs/whatsapp/business-management-api/get-started), иначе придется перевыпускать токен каждый день
+ Создайте приложение на [портале разработчиков](https://developers.facebook.com/apps/)
+ В панели подключите продукты Webhooks, WhatsApp
+ В админке THOTH - WABA - Add waba 
+ + name - имя вашего приложения 
+ + Access token - Постоянный или временный маркер
+ + После сохранения в списке WABA скопируйте Verify token для нужной учётки

+ На портале разработчиков - Quickstart > Configuration > 
+ + Callback URL - https://example.com/api/waba/?api-key=XXXXXXX
+ + Verify token - Verify token из предыдущего шага 
![alt text](img/verify.png)
+ В админке thoth - waba - phones и добавляем номера (Phone - номер, Phone id - id из приложения фейсбук)
+ Выбрать объект waba, созданный ранее
+ Выбрать App instance (портал битрикс) к котрому привязать номер waba
+ Отметьте Чекбокс "Sms service", если хотите зарегистрировать этот номер в качестве [СМС провайдера](messageservice.md)  
+ если все пройдет успешно, то в контакт центре коннектор станет зеленым и кнему будет прикрпелена линия THOTH_ваш_номер
![ok](img/waba_ok.png)

## Особенности работы WABA

+ Самое главное - первыми раз в сутки вы можете писать только используя заранее одобренный шаблон. Если линия (чат) уже создан можете отправить шаблон, используя конструкцию template-hello_world+en_US, где hello_world - назвение шаблона, en_US - язык шаблона. Так же можно отправить первое за сутки шаблонное сообщение через [SMS](messageservice.md)


# Активация SIP транка WhatsApp (приём звонков на сервер телефонии, напрмиер Asterisk)

https://developers.facebook.com/docs/whatsapp/cloud-api/calling

+ После подключения номера перейдите в настройки (в админке или пользовательском интерфейсе) и установите значение "Звонки" и "SIP" - Включено
+ Укажите адрес (example.com) вашего сервера (должен бфть настроен tls - трансопрт)
+ tls порт, по умолчанию 5061
+ Сохраните настройки. Если при активации приёма звонков с сервера Meta возникнет ошибка, её код будет отображаться в поле "Ошибка". 
Расшифровка кодов ошибок здесь - https://developers.facebook.com/docs/whatsapp/cloud-api/calling/troubleshooting/

### Пример настроек Asterisk (FreePBX) для приёма звонков из WhatsApp

```
# pjsip.transports.conf
[0.0.0.0-tls]
type=transport
protocol=tls
bind=0.0.0.0:5061
external_media_address=123.123.123.123
external_signaling_address=123.123.123.123
ca_list_file=/etc/ssl/certs/ca-certificates.crt
cert_file=/etc/asterisk/keys/example.com-fullchain.crt
priv_key_file=/etc/asterisk/keys/example.com.key
method=tlsv1_2
verify_client=no
verify_server=no
allow_reload=no
tos=cs3
cos=3
local_net=10.8.0.0/24


# pjsip.conf
[wa.meta.vc]
type=aor
qualify_frequency=60
contact=sip:wa.meta.vc

[wa.meta.vc]
type=endpoint
transport=0.0.0.0-tls
context=from-meta
disallow=all
allow=opus
aors=wa.meta.vc
send_connected_line=no
rtp_keepalive=0
language=en
user_eq_phone=no
t38_udptl=no
t38_udptl_ec=none
fax_detect=no
trust_id_inbound=no
t38_udptl_nat=no
direct_media=no
media_encryption=sdes
rtp_symmetric=yes
dtmf_mode=auto

[wa.meta.vc]
type=identify
endpoint=wa.meta.vc
match=69.171.251.0/255.255.255.0
```
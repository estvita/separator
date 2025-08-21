# FreePBX как шлюз для SIP-транка WhatsApp Business Calling API

FreePBX используется в качестве шлюза для SIP транка WhatsApp Business Calling API.

## Порядок подключения FreePBX к серверу thoth

Подробная инструкция: [FreePBX GraphQL Provisioning Tutorial](https://sangomakb.atlassian.net/wiki/spaces/FCD/pages/10354832/FreePBX+GraphQL+Provisioning+Tutorial)

### Шаги

1. Установить и активировать модуль **PBX API**
2. В модуле создать приложение **Machine-to-Machine** с правами   gql:framework gql:core
3. Записать `client_id` и `client_secret`
4. На сервере thoth в разделе `/admin/freepbx/server/` создать новый SIP сервер с `client_id` и `client_secret`
5. В настройках приложения WhatsApp Cloud (`/admin/waba/app/`) привязать созданный SIP сервер

---

## Настройка подключения

Дальнейшие настройки подключения выполняются на странице подключенного номера `/waba/`:

- Кликнуть по номеру  
- В блоке **"настройки звонков"** выбрать нужный вариант:

### Варианты подключения

1. **В Битрикс24**  
- Требуется приложение с правом `telephony` в дополнение к обычным
- На сервере FreePBX будет создан внутренний номер и входящий маршрут для номера WhatsApp на этот внутренний номер  
- В телефонии Битрикс будет создано SIP-подключение с данными этого внутреннего номера

2. **В SIP транк**  
- На сервере FreePBX будет создан внутренний номер и входящий маршрут для номера WhatsApp  
- Данные для подключения отображаются на странице

3. **На SIP сервер**  
- Указать свой сервер, настроенный на прием звонков от Меты 
         
     

### Пример настроек SIP транка Asterisk (FreePBX) для приёма звонков из WhatsApp

Получить все сети для сопоставления можно командой 
```
whois -h whois.radb.net -- '-i origin AS32934' | grep ^route: | awk '{print $2}' | grep -v ":"
```

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
match=69.171.224.0/19
```

**## Подключение WhatsApp Web к Битрикс24 (серая интеграция)**

Для подключения используется [Evolution API](https://github.com/EvolutionAPI/evolution-api).

Для работы сервиса интеграции требуется Celery.

**### Процесс настройки интеграции**

+ Запустите Evolution API согласно [инструкции](https://doc.evolution-api.com/v2/en/get-started/introduction).
+ В .env Evolution API установите переменные:
```
WEBHOOK_GLOBAL_URL='http://separator.url/api/waweb/?api-key=XXXX'
WEBHOOK_GLOBAL_ENABLED=true
WEBHOOK_GLOBAL_WEBHOOK_BY_EVENTS=false
WEBHOOK_EVENTS_APPLICATION_STARTUP=true
WEBHOOK_EVENTS_MESSAGES_SET=true
WEBHOOK_EVENTS_MESSAGES_UPSERT=true
WEBHOOK_EVENTS_CONNECTION_UPDATE=true
AUTHENTICATION_API_KEY=YYY
```

где:
+ separator.url — адрес установленного портала [separator](/README_ru.md).
+ XXXX — токен пользователя separator.
+ YYY — произвольный токен для авторизации в Evolution API.

**#### Настройки на стороне separator**
separator поддерживает работу с несколькими серверами Evolution API.
+ В админке separator создайте коннектор waweb.
+ Установите [локальное приложение в Битрикс](bitrix.ru.md).
+ В разделе waweb/server/ добавьте сервер Evolution API.
  + Server URL = SERVER_URL (Evolution API).
    > **Примечание для Docker:** Если Separator запущен в Docker контейнере, а Evolution API работает на хост-машине, для доступа к нему используйте адрес `http://host.docker.internal:PORT` (где PORT — порт Evolution API, например 8080 или 8085).
  + API Key = AUTHENTICATION_API_KEY (Evolution API)
  + max_connections — количество сессий WhatsApp на один сервер (по умолчанию 100). При достижении этого количества separator будет искать следующий сервер. Если он не добавлен в админке, при подключении будет выведено сообщение об отсутствии свободных серверов.

> **Совет:** Вы также можете добавить сервис Evolution API непосредственно в `docker-compose.yml` проекта Separator. В этом случае для подключения можно будет использовать имя сервиса (например, `http://evolution:8080`), и не потребуется настраивать доступ к хосту.

Пример `docker-compose.yml`:
```yaml
  evolution:
    image: evoapicloud/evolution-api:latest
    restart: always
    ports:
      - "8080:8080"
    volumes:
      - evolution_store:/evolution/store
      - evolution_instances:/evolution/instances
    environment:
      - AUTHENTICATION_API_KEY=YOUR_SECURE_TOKEN
      - SERVER_URL=http://localhost:8080
      # Add other required environment variables here
```

**### Подключение номера WhatsApp к Битрикс24**
Подключение осуществляется из пользовательского интерфейса по адресу /waweb/
+ При нажатии на кнопку "Добавить номер" создается сессия в Evolution API, из которой запрашивается QR-код.
+ Отсканируйте код через приложение WhatsApp на телефоне.
+ После успешного подключения приложения нажмите ссылку "вернуться" под QR-кодом.
+ В таблице со списком подключенных номеров выберите нужный портал Битрикс и подключите существующую линию или создайте новую.

После подключения в Битрикс24 будет создана новая открытая линия с названием, соответствующим номеру подключенного телефона, и [SMS-провайдер](messageservice.md).

SMS-провайдера можно отключить из админки, сняв чекбокс Sms service для нужного номера.
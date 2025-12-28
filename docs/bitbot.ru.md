[english](bitbot.md)

**BitBot — коннектор к Dify и Typebot для Битрикс24.**

BitBot подключает ботов из Dify (Chatflow / Workflow) и Typebot к Битрикс24 через Открытые линии.

---

#### 1. Настройка приложения в Bitrix24 и Separator

1. Создайте новое приложение в Битрикс24 и зарегистрируйте его в Separator.  
   Подробности см. в файле: `/docs/bitrix.md`.

2. Укажите минимальный scope:  
   `imbot`  
   При необходимости добавьте другие права (например, `task`, `crm`), если боту нужны REST‑запросы в ваш Битрикс24.

3. В настройках приложения Битрикс24 включите флаг `Bitbot`.

4. Укажите URL страницы приложения:  
   `/bitbot/`  
   Через этот интерфейс вы:
   - создаёте и настраиваете ботов;
   - добавляете и редактируете команды.  

   Через админку Django нельзя напрямую добавить бота или команду в Битрикс24.

---

#### 2. Провайдеры Dify и Typebot

В админ‑панели Separator создайте провайдер.

Типы провайдеров:

- `Dify Chatflow` (`dify_chatflow`)
- `Dify Workflow` (`dify_workflow`)
- `Typebot` (`typebot`)

Рекомендуемые базовые адреса:

- Dify: `https://api.dify.ai/v1`
- Typebot: `https://typebot.io`

В провайдере укажите:

- название (любое, будет отображаться как `Имя (Тип)`);
- тип;
- базовый URL сервиса.

---

#### 3. Какие данные передаются в бота

Из события Битрикс24 в ваш бот (Dify или Typebot) передаются переменные:

```text
user_access_token: XXX
bot_access_token: YYY
scope: task,entity,im,user_basic,log,calendar,disk,imbot,booking,documentgenerator
client_endpoint: https://b24-2zjuyu.bitrix24.kz/rest/

BOT_ID: 55
DIALOG_ID: chat7
MESSAGE_ID: 456
MESSAGE: text
AUTHOR_ID: 2
FIRST_NAME: Джон
LAST_NAME: Доу

COMMAND_ID: 12
COMMAND: help
COMMAND_PARAMS: text

CHAT_TITLE: client chat
LANGUAGE: en
CHAT_ENTITY_DATA_1: Y|DEAL|1|N|N|17|1765971491|0|0|0
CHAT_ENTITY_DATA_2: LEAD|0|COMPANY|0|CONTACT|1|DEAL|1
CHAT_ENTITY_ID: separator|3|7778889966|13
file_id: 48
file_type
```

Добавьте в вашем боте входные переменные с такими же именами.

Особенности:

- В Dify строковые поля по умолчанию ограничены 48 символами.  
  Увеличьте длину для полей вроде `access_token`, `COMMAND_PARAMS`, `CHAT_ENTITY_DATA_*`, иначе возможна ошибка:  
  `access_token in input form must be less than 48 characters`.

- Эти переменные можно использовать в логике бота для запросов к Битрикс24:
  чтение/запись данных, вызов REST‑методов и т.п.

- **Обработка файлов**: Если к сообщению в Битрикс24 прикреплён файл, переменная `file_id` будет передана боту с ID этого файла.  
  Вы можете использовать этот ID для скачивания файла через REST‑методы Битрикс24 (`disk.file.get`).  
  Если сообщение пусто, но к нему прикреплён файл, боту автоматически будет передано сообщение `"File sent: {имя_файла}"`.

---

#### 4. Подключение Dify

1. В админке создайте провайдера с типом `dify_chatflow` или `dify_workflow`.  
   Укажите базовый адрес сервера Dify (например, `https://api.dify.ai/v1` или ваш self‑hosted URL).

2. Создайте коннектор:
   - укажите API‑ключ Dify (`Developing with APIs`: <https://docs.dify.ai/en/use-dify/publish/developing-with-apis>);
   - при необходимости укажите свой базовый URL, если он отличается от URL в провайдере.

3. Создайте бота и при необходимости добавьте команды.

Поведение:

- Для `dify_chatflow`:
  - если есть `COMMAND`, ответ отправляется в Битрикс24 через `imbot.command.answer`;
  - если команды нет, используется `imbot.message.add`;
  - контекст диалога сохраняется в Redis по ключу  
    `bitbot:{member_id}:{BOT_ID}:{DIALOG_ID}`  
    время жизни ключа — 1 сутки.

- Для `dify_workflow`:
  - workflow запускается с `response_mode="blocking"`;
  - все `outputs` собираются в текст вида:  
    `имя_переменной: значение`;
  - ответ также отправляется либо как ответ на команду, либо как обычное сообщение — по той же логике, что для Typebot.

---

#### 5. Подключение Typebot

1. В админке создайте провайдера с типом `typebot` и укажите базовый адрес сервера:  
   `https://typebot.io`  
   (или ваш собственный адрес, если установлен self‑hosted Typebot).

2. В пользовательском интерфейсе `/bitbot/` создайте коннектор:
   - укажите API‑ключ Typebot;
   - укажите полный URL запуска бота, например:  
     `https://typebot.io/api/v1/typebots/basic-chat-gpt-xxxx/startChat`.

3. Создайте бота и добавьте команды при необходимости.

Поведение:

- При первом сообщении создаётся сессия, её `sessionId` сохраняется в Redis в ключ `bitbot:{member_id}:{BOT_ID}:{DIALOG_ID}` на 1 сутки.
- При последующих сообщениях используется  
  `https://typebot.io/api/v1/sessions/{sessionId}/continueChat`.
- Ответ формируется из поля `messages[].content.richText[*].children[].text`.
- Если событие — команда (`COMMAND` указана), ответ уходит через `imbot.command.answer`;  
  если обычное сообщение — через `imbot.message.add`.

---

#### 6. Подключение бота к Открытой линии Битрикс24

После настройки провайдера, коннектора и бота:

1. Откройте настройки Открытых линий в Битрикс24.
2. Подключите созданного чат‑бота к нужной линии.  
   Официальная инструкция: <https://helpdesk.bitrix24.com/open/25385203/>
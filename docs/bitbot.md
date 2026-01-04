[русский](bitbot.ru.md)

**BitBot is a connector between Bitrix24 and popular chatbot platforms (Dify and Typebot).**

BitBot allows you to use bots built in Dify (Chatflow / Workflow) and Typebot inside Bitrix24 Open Channels.

---

#### 1. Bitrix24 and Separator app setup

1. Create a new application in Bitrix24 and register it in Separator.  
   See `/docs/bitrix.md` for details.

2. Set at least the following scope:  
   `imbot`  
   Add more scopes (e.g. `task`, `crm`) if your bot needs to call Bitrix24 REST methods.

3. In Bitrix24 application settings, enable the `Bitbot` flag.

4. Set the application page URL to:  
   `/bitbot/`  

   This UI is used to:
   - create and configure bots;
   - add and manage bot commands.  

   You cannot create a bot or commands directly in Bitrix24 from the Django admin.

---

#### 2. Providers (Dify / Typebot)

In the Separator admin panel, create a Provider.

Supported provider types:

- `Dify Chatflow` (`dify_chatflow`)
- `Dify Workflow` (`dify_workflow`)
- `Typebot` (`typebot`)

Recommended default base URLs:

- Dify: `https://api.dify.ai/v1`
- Typebot: `https://typebot.io`

For each provider specify:

- name (it will be shown as `Name (Type)`);
- type;
- base URL.

---

#### 3. Input variables passed to the bot

The following variables from Bitrix24 events are passed to Dify / Typebot.  
Create corresponding input variables in your bot with the **same names**:

```text
user_access_token: XXX
bot_access_token: YYY
scope: task,entity,im,user_basic,log,calendar,disk,imbot,booking,documentgenerator
client_endpoint: https://b24-2zjuyu.bitrix24.kz/rest/

BOT_ID: 55
DIALOG_ID: chat7
CHAT_ID: 7
MESSAGE_ID: 456
MESSAGE: text
AUTHOR_ID: 2
FIRST_NAME: John
LAST_NAME: Dou

COMMAND_ID: 12
COMMAND: help
COMMAND_PARAMS: text

CHAT_TITLE: client chat John Dou
LANGUAGE: en
CHAT_ENTITY_DATA_1: Y|DEAL|1|N|N|17|1765971491|0|0|0
CHAT_ENTITY_DATA_2: LEAD|0|COMPANY|0|CONTACT|1|DEAL|1
CHAT_ENTITY_ID: separator|3|7778889966|13
file_id: 48
file_type
```

Notes:

- In Dify, string variables are limited to 48 characters by default.  
  Increase the max length of variables such as `access_token`, `COMMAND_PARAMS`, `CHAT_ENTITY_DATA_*`, otherwise you may get:  
  `access_token in input form must be less than 48 characters`.

- You can use `access_token`, `client_endpoint` and other values in your flow to call Bitrix24 REST API.

- **File handling**: If a file is attached to the message in Bitrix24, the `file_id` variable will be passed to the bot.  
  You can use this ID to download the file via Bitrix24 REST API method `disk.file.get` 
  If the message is empty but a file is attached, the bot will receive the message `"File sent: {filename}"` automatically.

---

#### 4. Dify integration

1. In the admin panel create a provider of type `dify_chatflow` or `dify_workflow`.  
   Set the base URL of your Dify instance, for example:  
   `https://api.dify.ai/v1` (cloud) or your self‑hosted URL.

2. Create a connector:
   - set your Dify API key (see:  
     <https://docs.dify.ai/en/use-dify/publish/developing-with-apis>);
   - optionally override the base URL if it differs from the provider URL.

3. Create a bot and add commands if needed.

Behaviour:

- For `dify_chatflow`:
  - if the incoming event contains `COMMAND`, the response is sent via `imbot.command.answer`;
  - otherwise via `imbot.message.add`;
  - conversation context is stored in Redis under  
    `bitbot:{member_id}:{BOT_ID}:{DIALOG_ID}`  
    with TTL = 1 day.

- For `dify_workflow`:
  - the workflow runs with `response_mode="blocking"`;
  - all `outputs` are converted into text like:  
    `variable_name: value`;
  - the text is sent either as a command reply or a regular message (same logic as in the Typebot part).

---

#### 5. Typebot integration

1. In the admin panel create a `typebot` provider and set the base URL:  
   `https://typebot.io`  
   (or your own URL for self‑hosted Typebot).

2. In the `/bitbot/` UI create a connector:
   - set your Typebot API key;
   - set the full start URL of your bot, for example:  
     `https://typebot.io/api/v1/typebots/basic-chat-gpt-xxxx/startChat`.

3. Create a bot and add commands if needed.

Behaviour:

- On the first message a new session is created; `sessionId` is saved in Redis under  
  `bitbot:{member_id}:{BOT_ID}:{DIALOG_ID}` with 1‑day TTL.
- Next messages use  
  `https://typebot.io/api/v1/sessions/{sessionId}/continueChat`.
- The reply text is read from `messages[].content.richText[*].children[].text`.
- If `COMMAND` is present, the reply is sent with `imbot.command.answer`;  
  otherwise `imbot.message.add` is used.

---

#### 6. Connecting the bot to a Bitrix24 Open Channel

1. Open the Open Channel settings in Bitrix24.
2. Attach your BitBot‑based chatbot to the desired Open Channel.  
   See the official Bitrix24 docs: <https://helpdesk.bitrix24.com/open/25385203/>
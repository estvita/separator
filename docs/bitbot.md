In the Bitrix application settings, set the 'Bitbot' flag.

user url - /bitbot/

Add the provider to the admin panel by specifying the address

(defaul addresses)
+ Dify: https://api.dify.ai/v1
+ Typebot: https://typebot.io/api/v1/typebots

to dify chatflow / workflow the following variables from Bitrix24 event are passed

Add the input fields you need to the node "User input"

Please note that by default string fields are created with a size of 48 characters, you will need to increase this value

Only Dify chatflow sends a response to Bitrix. Workflows can be used to execute commands from Bitrix24 chats

```
event
scope
access_token
client_endpoint
BOT_ID
DIALOG_ID
AUTHOR_ID
MESSAGE
COMMAND
COMMAND_PARAMS
CHAT_TITLE
LANGUAGE
CHAT_ENTITY_DATA_1
CHAT_ENTITY_DATA_2
FIRST_NAME
LAST_NAME
``` 

After creating the bot, connect it in the Bitrix24  [open line settings](https://helpdesk.bitrix24.com/open/25385203/)
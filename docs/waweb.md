**## Connecting WhatsApp Web to Bitrix24 (grey integration)**

The [Evolution API](https://github.com/EvolutionAPI/evolution-api) is used for the connection.

Celery is required for the integration service.

**### Integration Setup Process**

+ Run Evolution API according to the [instructions](https://doc.evolution-api.com/v2/en/get-started/introduction)
+ Set the following variables in Evolution API .env:
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

where
+ separator.url = the address of the installed [separator] portal (/README_ru.md)
+ XXXX - separator user token
+ YYY - any token for authentication in Evolution API

**#### separator Side Settings**
separator supports working with multiple Evolution API servers
+ In separator admin panel, create a waweb connector
+ Install [local app in Bitrix](bitrix.md)
+ In the waweb/server/ section, add an Evolution API server
  + Server URL = SERVER_URL (Evolution API)
    > **Note for Docker:** If Separator is running in a Docker container and Evolution API is running on the host machine, use `http://host.docker.internal:PORT` (where PORT is the Evolution API port, e.g., 8080 or 8085) for the Server URL.
  + API Key = AUTHENTICATION_API_KEY (Evolution API)
  + max_connections – number of WhatsApp sessions per server (default is 100). When this number is reached, separator will look for the next server; if it is not added in the admin panel, a message about the absence of free servers will be displayed when connecting

> **Tip:** You can also add the Evolution API service directly to the `docker-compose.yml` of the Separator project. In this case, you can use the service name (e.g., `http://evolution:8080`) for connection, and no host access configuration is required.

Example `docker-compose.yml` service configuration:
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

**### Connecting a WhatsApp Number to Bitrix24**
The connection is done from the user interface at /waweb/
+ When you click the "Add number" button, a session is created in Evolution API and a QR code is requested from it
+ Scan the code through the WhatsApp app on your phone
+ After the app connects successfully, click the "return" link under the QR code
+ In the table with the list of connected numbers, select the required Bitrix portal and connect an existing line or create a new one

After connection, a new open line will be created in Bitrix24 with a name corresponding to the connected phone number and an [SMS provider](messageservice.md)

The SMS provider can be disabled in the admin panel by unchecking the Sms service checkbox for the desired number

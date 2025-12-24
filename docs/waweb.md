**## Connecting WhatsApp Web to Bitrix24 (grey integration)**

The [Evolution API](https://github.com/EvolutionAPI/evolution-api) is used for the connection.

Celery is required for the integration service.

**### Integration Setup Process**

For convenience, a separate configuration file `docker-compose.evolution.yml` is provided to run Evolution API alongside Separator in Docker.

1.  **Create the `.env.evolution` configuration file**
    Copy the example configuration:
    ```bash
    cp docs/example/env.evolution.example .env.evolution
    ```
    Edit `.env.evolution` and set your `AUTHENTICATION_API_KEY`. The other settings are already optimized for running within the Separator Docker network.

2.  **Start the services**
    Use the following command to start Separator together with Evolution API:
    ```bash
    docker compose -f docker-compose.yml -f docker-compose.evolution.yml up -d
    ```

**#### Settings on the Separator side**
Separator supports working with multiple Evolution API servers.

+ In the Separator admin panel, create a `waweb` connector.
+ Install the [local application in Bitrix](bitrix.md).
+ In the `waweb/server/` section, add the Evolution API server.
  + **Server URL**: `http://evolution:8080` (internal Docker address)
  + **API Key**: Your key from `AUTHENTICATION_API_KEY` (in the `.env.evolution` file)
  + **max_connections**: Number of WhatsApp sessions per server (default is 100).

> **Note:** Thanks to the internal Docker network, Separator automatically trusts requests from Evolution API, so you do not need to specify an API key in the webhook settings (`WEBHOOK_GLOBAL_URL`).

**### Connecting a WhatsApp Number to Bitrix24**
The connection is done from the user interface at /waweb/
+ When you click the "Add number" button, a session is created in Evolution API and a QR code is requested from it
+ Scan the code through the WhatsApp app on your phone
+ After the app connects successfully, click the "return" link under the QR code
+ In the table with the list of connected numbers, select the required Bitrix portal and connect an existing line or create a new one

After connection, a new open line will be created in Bitrix24 with a name corresponding to the connected phone number and an [SMS provider](messageservice.md)

The SMS provider can be disabled in the admin panel by unchecking the Sms service checkbox for the desired number

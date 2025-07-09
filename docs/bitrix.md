**## Connecting the Bitrix24 Portal**

Video tutorial (based on previous version) - https://youtu.be/ti99AeGAr4k

**#### Preparing the application on the thoth server**

+ In the Sites section, rename example.com to the domain through which thoth will be accessed
+ In Bitrix > Connectors section, add a connector with an SVG icon
+ Bitrix > Apps - add an app. Enter the name (waba, waweb, olx) and select a domain, choose the necessary connectors
+ Fill in the "Page url" field with a link to the application settings page. For example (/waweb/, /waba/), this page will open in Bitrix24 when using web interface installation.

**#### Preparing the application in Bitrix24**
+ In Bitrix24, create a server local application (Applications – For Developers – Other – Local Application) in Bitrix24 and fill in the relevant fields (Your handler path and Initial installation path)
+ Required permissions (Permission setup): crm, imopenlines, contact_center, user, messageservice, im, imconnector, disk

![b24 local app](img/b24_local_app.png)

**### Installing the application with a web interface**
After installation of this type, your application will appear in the left menu, and clicking it will open the page specified in "Page url"

In the local application's settings in Bitrix24, in addition to previous steps:

+ In the "Your handler path" field - https://example.com/app-settings/
+ In the "Initial installation path" field - https://example.com/app-install/
+ Fill in the "Menu item" field
+ Click "Install", then paste the obtained client_id and client_secret into the corresponding fields in the application on the thoth server
+ In Bitrix24 local application, click the "Go to application" button, if everything is correct, you will see the page specified in the "Page url" field

**### Installing the application without a web interface**

+ In the admin panel, create a token

![thoth user token](img/token.png)

+ After saving the record, copy the Id displayed in the list of applications.

In the local application's settings in Bitrix24, in addition to previous steps:

+ In the "Your handler path" field -  https://example.com/api/bitrix/?api-key=XXXXXXX&app-id=YYYYYYY
+ In the "Initial installation path" field - https://example.com/api/bitrix/?api-key=XXXXXXX
+ Check the "Uses API only" checkbox

where XXXXXXX is your token, YYYYYYY is the app id from the previous step

+ Click "Install", then paste the obtained client_id and client_secret into the relevant fields in the application on the thoth server
+ In Bitrix in the "Contact Center" section, connectors should appear

![alt text](img/olx-connector.png)
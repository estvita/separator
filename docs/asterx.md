# Integration CRM Bitrix24 with Asterisk-based IP PBX

The [AsterX](https://github.com/estvita/AsterX) connector operates by interacting with the PBX through the AMI (Asterisk Manager Interface). Thus, Bitrix24 integration is possible with any PBX running on Asterisk:

Software PBXs:
+ FreePBX (the most common with GUI)
+ Issabel (Elastix-Style)
+ VitalPBX
+ "Pure" Asterisk (manual configuration)
+ and others

Hardware solutions on Asterisk with AMI support (depends on the model)
+ Yeastar
+ Grandstream
+ OpenVox 
+ and others

The AsterX connector can operate independently or be managed by the separator server.

Main features:

+ Call filtering based on context assignment  
+ Call registration in Bitrix24 (telephony.externalcall.register)  
+ Displaying the client card (telephony.externalcall.show)  
+ Call completion (telephony.externalcall.finish)  
+ Attaching call recording (telephony.externalCall.attachRecord)  
+ ClickToCall support (OnExternalCallStart)  
+ Callback form support (OnExternalCallBackStart)  
+ Automatic connection to manager (no need to edit the dialplan)  
+ Uploading voicemail messages to Bitrix24  
+ Ability to connect an unlimited number of PBXs to a single Bitrix24 portal  


## Installation

On Bitrix and separator side:
+ In .env separator set ASTERX_SERVER=True
+ Install additional packages from ![asterx.txt](/requirements/asterx.txt)
+ Create a local Bitrix24 application, specify addresses: https://example.com/app-install/ and https://example.com/app-settings/, set permissions crm, user, disk, telephony, im. Click "Save" and save client_id, client_secret
![asterx_b24](/docs/img/asterx_b24.png)
+ Create an application in the separator interface with ONAPPUNINSTALL, ONEXTERNALCALLSTART, ONEXTERNALCALLBACKSTART events. Check the "AsterX" box. Page url: /asterx/. Fill in client_id, client_secret with values from the previous step.
+ Click "Go to application" in Bitrix24
+ In the opened interface, click "Add PBX"
![add_pbx](/docs/img/add_pbx.png)
+ Copy the received PBX-ID: XXXXXX
+ Click on the PBX name and go to settings, from the dropdown select the AsterX application on ... your Bitrix portal, Save

On PBX side:
For software PBXs, it’s recommended to install the connector on the server itself; for hardware solutions, use an external server.

+ Create an AMI user

AsterX setup:
```
git clone https://github.com/estvita/asterx.git
cd asterx
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/local_sql.txt
cp examples/cloud.ini config.ini
nano config.ini
```

```
[app]
control_server_http = https://example.com
control_server_ws = wss://example.com


[asterisk]
pbx_id = XXXXXX
host = localhost
port = 5038
username = AMI-username
secret = AMI-secret
```

Run in test mode: python main.py

After connector authorization on the separator server, keys and basic settings will be sent to the connector database.

In turn, the connector will send the server a list of contexts, from which you need to select external, internal, and ignored in the Bitrix24 PBX settings interface.

Important: both incoming and outgoing calls involve two contexts; thus, there must be at least one external and one internal line set in the settings.

![asterx_settings](/docs/img/asterx_settings.png)

To exclude calls from statistics, select "Exclude"

To match calls between Bitrix24 users and PBX, assign each user's extension in the PBX to their Bitrix24 telephony user profile, and set the AsterX app as the default number.

After any change to PBX user settings in the application interface, click "Update" in the PBX list — the local connector user database will be recreated.

For production use: daphne -b 0.0.0.0 -p 8000 config.asgi:application

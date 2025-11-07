## Connecting WhatsApp (WABA)

Works with Login Flow Logic https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/

+ It is recommended to obtain a [Permanent Token](https://developers.facebook.com/docs/whatsapp/business-management-api/get-started), otherwise you will need to regenerate the token every day.  
+ Create an app in the [Developer Portal](https://developers.facebook.com/apps/)  
+ In the app dashboard, enable the products: **Webhooks, WhatsApp**  
+ In the separator admin panel → **WABA → Add App**
  + **Events** - If checked, all incoming events will be saved to the database (for debugging)
  + **Site** – Select site. The website domain must match the domain on which the application will be used. 1 website - 1 application
  + **Client id** – from fb App Settings > Basic  > App ID
  + **Client secret** – from fb App Settings > Basic  > App secret  
  + **Access token** – permanent or temporary token  
  + After saving, in the WABA list copy the **Verify token** for the desired account


+ In the Developer Portal → **Webhooks**  
  + **Select product** - Whatsapp Business Account
  + **Callback URL** – `https://example.com/api/waba/?api-key=XXXXXXX`  
  + **Verify token** – the verify token from the previous step and click "Verify and save" button
  + **Webhook fields** – check "message_template_components_update", "message_template_status_update", "messages", "account_update"
![alt text](img/verify.png)

+ In the separator admin panel → **waba → waba**, add waba object:
  + **App** - select App
  + **Waba id** - paste WABA ID from FB > WhatsApp > Quickstart > API Setup > WhatsApp Business Account ID
  + **Access token** permanent tocken from first step

+ In the separator admin panel → **waba → phones**, add phone numbers:  
  + **Phone** – the phone number  
  + **Phone id** – the id from the Facebook app  WhatsApp > Quickstart > API Setup > Phone number ID
+ Select the previously created WABA object  
+ Select the **App instance** (Bitrix portal) to which the WABA number will be linked  
+ Check the **Sms service** checkbox if you want to register this number as an [SMS provider](messageservice.md)  
+ If everything is set up correctly, the connector in the Contact Center will turn green, and the line `separator_your_number` will be attached.  
![ok](img/waba_ok.png)

## WABA Usage Notes

+ The most important rule – the first message per day can only be sent using a pre-approved template.  
  If a line (chat) already exists, you can send a template using the format:  
  `template-hello_world+en_US`, where `hello_world` is the template name, and `en_US` is the template language.  
  You can also send the first daily template message via [SMS](messageservice.md).

### Templates with variable
Now support only number variable type

if you added a template with variables
```
Hi {{1}},

Your new account has been created successfully. 

Please verify {{2}} to complete your profile.
```
To send this template from SMS or Open Lines to Bitrkis24, you can use the following structure:

template-hello_world+en_US+value1|value2

where "value1" and "value2" are the text of your variables.

That is, to transfer the first variable, add + to the code and the text of the first variable. Separate the value of each subsequent variable with a pipe |.

### Templates with media
Now support document, image and video

template-hello_world+file_link:https://file_link.url

template-hello_world+en_US+file_link:https://file_link.url|value1|value2

# WhatsApp Cloud API SIP Trunk Activation (receiving calls on a telephony server, e.g. Asterisk)

https://developers.facebook.com/docs/whatsapp/cloud-api/calling  

+ After connecting the number, go to the settings (in the admin panel or user interface) and set both **"Calling"** and **"SIP"** to *Enabled*.  
+ Specify the address (e.g. `example.com`) of your server (TLS transport must be configured).  
+ TLS port: default is **5061**.  
+ Save the settings. If an error occurs during activation of incoming calls from Meta’s server, the error code will be displayed in the **Error** field.  
  Error codes explained here: https://developers.facebook.com/docs/whatsapp/cloud-api/calling/troubleshooting/  
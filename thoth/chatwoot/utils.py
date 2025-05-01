import requests
import random
import string
import re
from rest_framework.authtoken.models import Token
from django.contrib.sites.models import Site
from rest_framework.response import Response

from django.conf import settings
from django.core.mail import send_mail
from .models import Chatwoot, User, Account, PhoneNumber, AgentBot, Feature, Limit

CHATWOOT_ID = settings.CHATWOOT_ID
SITE_ID = settings.SITE_ID


class ChatwootClient:
    def __init__(self, account_id):

        self.account_id = account_id
        self.chatwoot = Chatwoot.objects.get(id=CHATWOOT_ID)
        self.user = User.objects.get(account__id=self.account_id)
        self.base_url = f"{self.chatwoot.url}api/v1/accounts/{self.account_id}/conversations"
        self.headers = {"api_access_token": self.user.access_token}

    def get_conversations_labels(self, conversation_id):
        url = f"{self.base_url}/{conversation_id}/labels"
        try:
            resp = requests.get(url, headers=self.headers)
            resp.raise_for_status()
            return resp.json().get("payload", [])
        except requests.RequestException as e:
            return "error"

    def remove_conversation_label(self, conversation_id, label):
        # Получаем текущие метки
        labels = self.get_conversations_labels(conversation_id)
        if labels == "error":
            return "Не удалось получить текущие метки"
        
        # Удаляем переданную метку, если она существует в списке
        if label in labels:
            labels.remove(label)
        else:
            return f"Метка {label} не найдена в текущем списке"
        
        url = f"{self.base_url}/{conversation_id}/labels"
        
        try:
            resp = requests.post(url, json={"labels": labels}, headers=self.headers)
            resp.raise_for_status()
            return f"Метка {label} успешно удалена."
        except requests.RequestException as e:
            return "Возникла ошибка, попробуйте позже"        
    
    def updtae_contact(self, contact_id, params):
        url = f"{self.chatwoot.url}api/v1/accounts/{self.account_id}/contacts/{contact_id}"
        return requests.put(url, json=params, headers=self.headers)


def call_api(url, data=None, files=None, access_token=None, method="post"):
    chatwoot = Chatwoot.objects.get(id=CHATWOOT_ID)
    api_url = f"{chatwoot.url}{url}"
    if not access_token:
        access_token = chatwoot.platform_key
    headers = {"api_access_token": access_token}

    return requests.request(method, api_url, json=data, files=files, headers=headers)


def generate_password(length=12):

    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    special = string.punctuation

    password = [
        random.choice(lower),
        random.choice(upper),
        random.choice(digits),
        random.choice(special),
    ]

    all_characters = lower + upper + digits + special
    password += random.choices(all_characters, k=length - 4)

    random.shuffle(password)
   
    return ''.join(password)


def create_bot(user, bot_name, id_bot, type="bot"):
    site = Site.objects.get(id=SITE_ID)
    chatwoot_user = User.objects.get(owner=user)
    account_id = chatwoot_user.account.id
    token, created = Token.objects.get_or_create(user=chatwoot_user.owner)
    url = f"api/v1/accounts/{account_id}/agent_bots"

    payload = {
        "name": bot_name,
        "outgoing_url": f"https://{site.domain}/api/{type}/?id={id_bot}&api-key={token}"
    }
    try:
        resp = call_api(url, data=payload, access_token=chatwoot_user.access_token)
        resp.raise_for_status()
        bot_data = resp.json()
        bot_id = bot_data.get("id")

        # Save bot data to the local database
        agent_bot = AgentBot.objects.create(
            id=bot_id,
            token=bot_data.get('access_token'),
            account=chatwoot_user.account
        )

        return agent_bot
    except Exception as e:
        print(e)
        return {"error": str(e)}
    

def delete_bot(user, bot_id):
    chatwoot_user = User.objects.get(owner=user)
    account_id = chatwoot_user.account.id

    url = f"api/v1/accounts/{account_id}/agent_bots/{bot_id}"
    return call_api(url, access_token=chatwoot_user.access_token, method="delete")


def create_chatwoot_user(email, user):
    chatwoot = Chatwoot.objects.get(id=CHATWOOT_ID)
    url = f"platform/api/v1/"

    password = generate_password()

    # Create Chatwoot user
    user_payload = {
        "email": email,
        "password": password,
        "name": email
    }

    create_user = call_api(f"{url}users", user_payload)
    if create_user.status_code != 200:
        print("Failed to create user in Chatwoot", create_user.json())
        return {"error": "Failed to create user in Chatwoot"}

    user_data = create_user.json()
    user_id = user_data.get('id')
    access_token = user_data.get('access_token')

    # Fetch features and limits from the database
    features = Feature.objects.filter(server=chatwoot)
    limits = Limit.objects.filter(server=chatwoot)

    # Prepare features dictionary
    features_dict = {feature.name: feature.is_enabled for feature in features}

    # Prepare limits dictionary
    limits_dict = {limit.name: limit.value for limit in limits}

    # Create Chatwoot account
    account_payload = {
        'name': email,
        'locale': 'ru',
        "support_email": email,
        "features": features_dict,
        "limits": limits_dict
    }

    create_account = call_api(f'{url}accounts', account_payload)
    if create_account.status_code != 200:
        print("create_account", create_account.json())
        return {"error": "Failed to create account in Chatwoot"}

    account_data = create_account.json()
    account_id = account_data.get('id')

    account, created = Account.objects.update_or_create(
        owner=user,
        id=account_id
    )

    if chatwoot.default_role:
        user_role = chatwoot.default_role
    else:
        user_role = "agent"

    # Assign user to the account
    account_user_payload = {
        'user_id': user_id,
        'role': user_role
    }

    account_user = call_api(f'{url}accounts/{account_id}/account_users', data=account_user_payload)

    if account_user.status_code != 200:
        return {"error": "Failed to assign user to account"}

    # Save user data in the database
    chatwoot_user, created = User.objects.update_or_create(
        owner=user,
        defaults={
            'id': user_id,
            'account': account,
            'access_token': access_token
        }
    )

    # Send email with account credentials
    send_mail(
        subject="Welcome to chat.thoth.kz!",
        message=f"Hello,\n\nYour chat account has been successfully created. \n\n Your login: {email}\n Your password: {password}\n\nBest regards,\nYour Team",
        from_email=settings.EMAIL_HOST_USER,
        recipient_list=[email],
        fail_silently=False,
    )

    return {"success": "Chatwoot user and account created successfully"}


def add_inbox(user, payload):
    try:
        chatwoot_user = User.objects.get(owner=user)
        account_id = chatwoot_user.account.id

        # проверка роли пользователя
        user_url = f'platform/api/v1/users/{chatwoot_user.id}'
        user_data = call_api(user_url, method="get")
        user_data = user_data.json()

        user_role = user_data.get('role')
        if user_role == 'agent':
            app_url = f'platform/api/v1/accounts/{account_id}/account_users'
            resp = call_api(app_url, data={'user_id': chatwoot_user.id, 'role': 'administrator'})
            
            if resp.status_code != 200:
                print('account_users', resp.json())
                return resp.json()

        # Добавляем номер
        acc_url = f"api/v1/accounts/{account_id}/"

        inbox_data = call_api(f"{acc_url}inboxes/", data=payload, access_token=chatwoot_user.access_token)
        if inbox_data.status_code != 200:
            print('inbox_data', inbox_data.json())
            return {"error": "Failed to create Inbox"}
        
        inbox_data = inbox_data.json()
        inbox_id = inbox_data.get('id')
        
        agent_data = {
            'inbox_id': inbox_id,
            'user_ids': [chatwoot_user.id]
        }
        agent_add = call_api(f'{acc_url}inbox_members', data=agent_data, access_token=chatwoot_user.access_token)
        if agent_add.status_code != 200:
            print('inbox_data', agent_add.json())
            return {"error": "Failed to add Agent Inbox"}
        
        # Забираем права админа
        if user_role == 'agent':
            resp = call_api(app_url, data={'user_id': chatwoot_user.id, 'role': 'agent'})

        return {"result": {"inbox_id": inbox_id, "account": chatwoot_user.account}}
    except User.DoesNotExist:
        return


def whatsapp_webhook(data, phone_number):
    chatwoot = Chatwoot.objects.get(id=CHATWOOT_ID)
    try:
        url = f'{chatwoot.url}webhooks/whatsapp/+{phone_number}'
        resp = requests.post(url, json=data)
        resp.raise_for_status()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}
    

def get_sso_link(user):
    chatwoot = Chatwoot.objects.get(id=CHATWOOT_ID)
    try:
        if user is None or not user.is_authenticated:
            return chatwoot.url

        try:
            chatwoot_user = User.objects.get(owner=user)
        except User.DoesNotExist:
            email = user.email
            create_chatwoot_user(email, user)
            chatwoot_user = User.objects.get(owner=user)
        
        user_id = chatwoot_user.id
        sso_url = f"platform/api/v1/users/{user_id}/login"

        response = call_api(sso_url, method="get")

        if response.status_code == 200:
            data = response.json()
            return data.get("url")
    except Exception as e:
        print(f"Ошибка при получении SSO ссылки: {e}")

    return chatwoot.url


def get_contact(user, phone):
    cleaned_phone = re.sub(r'\D', '', phone)
    PhoneNumber.objects.get_or_create(phone=cleaned_phone)
    chatwoot_user = User.objects.get(owner=user)

    url = f"api/v1/accounts/{chatwoot_user.account.id}/contacts"

    payload = {
        'name': cleaned_phone,
        'phone_number': f"+{cleaned_phone}"
    }

    resp = call_api(url, data=payload, access_token=chatwoot_user.access_token)
    contact_data = resp.json()

    # Обработка случая "Phone number has already been taken"
    if 'message' in contact_data:
        message = contact_data.get('message')
        if message == 'Phone number has already been taken':
            search_url = f"{url}/filter"
            payload = {
                "payload": [
                    {
                        "attribute_key": "phone_number",
                        "filter_operator": "contains",
                        "values": [
                            cleaned_phone[-10:]
                        ]
                    }
                ]
            }
            search_resp = call_api(search_url, data=payload, access_token=chatwoot_user.access_token)
            search_data = search_resp.json()
            contact_data = search_data.get('payload', [])[0]
    else:
        payload = contact_data.get('payload', {})
        contact_data = payload.get('contact', {})
            
    contact_id = contact_data.get('id')

    return contact_id


def get_id_by_inbox_id(data, target_inbox_id):
    for item in data:
        if item.get("inbox_id") == int(target_inbox_id):
            return item.get("id")
    return None


def send_api_message(inbox, data):
    chatwoot_user = User.objects.get(owner=inbox.owner)
    url = f"api/v1/accounts/{chatwoot_user.account.id}/"

    remoteJid = data.get('remoteJid')
    contact_id = get_contact(inbox.owner, remoteJid)
    if contact_id is None:
        return Response({'error': "contact not found"}, status=400)
    message_type = "outgoing" if data.get('fromme') else "incoming"
    
    conv_url = f"{url}contacts/{contact_id}/conversations"
    conv_resp = call_api(conv_url, method="get", access_token=chatwoot_user.access_token)
    if conv_resp.status_code != 200:
        return
    conv_data = conv_resp.json()
    result_payload = conv_data.get('payload')
    conv_id = get_id_by_inbox_id(result_payload, inbox.id)
    if not conv_id:
    
        conv_payload = {
            "inbox_id": inbox.id,
            "contact_id": contact_id
        }
        conv_resp = call_api(f"{url}conversations", data=conv_payload, access_token=chatwoot_user.access_token)
        if conv_resp.status_code != 200:
            return
        conv_data = conv_resp.json()
        conv_id = conv_data.get('id')

    msg_url = f"{url}conversations/{conv_id}/messages"
   
    if data.get('attachments'):
        payload = {
            'attachments[]': data.get('attachments'),
            'content': (None, data.get('content')),
            'message_type': (None, message_type),
        }
        return call_api(msg_url, files=payload, access_token=chatwoot_user.access_token)
    else:
        payload = {
            "message_type": message_type,
            "content": data.get('content'),
        }

        return call_api(msg_url, data=payload, access_token=chatwoot_user.access_token)


def bot_handoff(url, token):
    try:
        resp = call_api(url, data={"status": "open"}, access_token=token)
        if resp.status_code == 200:
            return "Перевод на оператора успешен"
        else:
            return "Перевод на оператора не успешен"
    except requests.RequestException:
        return "Перевод на оператора не успешен"
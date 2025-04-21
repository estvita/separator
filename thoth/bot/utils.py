import requests
from django.conf import settings
from thoth.chatwoot.models import Chatwoot, User
from thoth.bitrix.crest import call_method

CHATWOOT_ID = settings.CHATWOOT_ID
SONET_GROUP_ID = settings.SONET_GROUP_ID

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
      

def bitrix_user_add(bot, email, account_id, conversation_id, contact_id):
    chatwoot_client = ChatwootClient(account_id=account_id)
    chatwoot_client.updtae_contact(contact_id, {"email": email})
    payload = {
        "EMAIL": email,
        "SONET_GROUP_ID": SONET_GROUP_ID,
        "EXTRANET": "Y",
    }
    resp = call_method(bot.bitrix, "user.add", payload)
    if "result" in resp:
        chatwoot_client.remove_conversation_label(conversation_id, bot.follow_up)
        return "Приглашение успешно отправлено, проверьте вашу почту"
    elif "error" in resp:
        return f"ERROOR!! Кандидат не приглашен. Текст ошибки {resp.get('error_description')}"
    

def get_tools_for_bot(user, bot, engine):
    tools = []

    features = bot.features.filter(type="function", engine=engine)

    for feature in features:
        # Добавляем публичные функции или приватные, владельцем которых является пользователь
        if feature.privacy == "public" or feature.owner == user:
            # Если description_openai уже является списком (например, [{"type": "function", ...}, ...])
            if isinstance(feature.description_openai, list):
                tools.extend(feature.description_openai)
            else:
                tools.append(feature.description_openai)

    return tools
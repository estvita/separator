import random
import requests
from celery import shared_task
from django.conf import settings
from thoth.waba.models import Phone
from .models import Extension

class PbxClient:
    def __init__(self):
        self.app_id = settings.WABA_APP_ID
        self.app = None
        self.server = None
        self.apps = settings.INSTALLED_APPS

    def load_app_and_server(self):
        from thoth.waba.models import App
        try:
            self.app = App.objects.get(id=self.app_id)
        except Exception:
            self.app = None
            self.server = None
            return
        self.server = getattr(self.app, "sip_server", None)

    def fetch_access_token(self):
        if self.app is None or self.server is None:
            self.load_app_and_server()
        if not self.server:
            raise Exception("SIP Server not connected to WA APP")
        data = {
            "grant_type": "client_credentials",
            "scope": self.server.gql_scopes,
        }
        try:
            resp = requests.post(
                f"https://{self.server.domain}/admin/api/api/token",
                data=data,
                auth=(self.server.client_id, self.server.client_secret),
                timeout=20,
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
            return token
        except requests.exceptions.RequestException:
            raise Exception(resp.json())
    
    def generate_unique_number(self):
        digits = self.server.ext_digits
        min_num = 10 ** (digits - 1)
        max_num = 10 ** digits - 1
        used_numbers = set(Extension.objects.filter(server=self.server).values_list('number', flat=True))
        all_numbers = set(range(min_num, max_num + 1))
        available_numbers = all_numbers - used_numbers
        if not available_numbers:
            raise Exception("Error: numbers is not available")
        return random.choice(list(available_numbers))

    def create_extension(self, phone_id):
        waba_phone = Phone.objects.get(id=phone_id)
        phone = waba_phone.phone
        phone = ''.join(filter(str.isdigit, str(phone)))
        phone = f"+{phone}"
        token = self.fetch_access_token()
        ext = self.generate_unique_number()

        url = f"https://{self.server.domain}/admin/api/api/gql"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        gqlAddUser = """
        mutation {
            addExtension(
                input: {
                    extensionId: %s
                    name: "%s"
                    email: "%s"
                    umEnable: false
                    vmEnable: false
                    maxContacts: "1"
                }
            ) {
                status
                message
            }
        }
        """ % (ext, ext, waba_phone.owner.email)

        data_ext = {"query": gqlAddUser}
        try:
            resp_ext = requests.post(url, json=data_ext, headers=headers)
            resp_ext.raise_for_status()
            resp_ext = resp_ext.json()
        except requests.exceptions.RequestException:
            raise Exception(phone, resp_ext.json())
        
        gqlfetchExtension = """
        {
        fetchExtension(extensionId: %s) {
            status
            id
            extensionId
            user {
                extPassword
            }
        }
        }
        """ % (ext)

        fetch_ext = {"query": gqlfetchExtension}
        try:
            resp_fetch = requests.post(url, json=fetch_ext, headers=headers)
            resp_fetch.raise_for_status()
            resp_fetch = resp_fetch.json()
            data = resp_fetch.get("data", {}).get("fetchExtension", {})
            ext_user = data.get("user", {})
            password = ext_user.get("extPassword")
        except requests.exceptions.RequestException:
            raise Exception(phone, resp_fetch.json())
        
        date_end = None
        if "thoth.tariff" in self.apps:
            from thoth.tariff.utils import get_trial
            date_end = get_trial(waba_phone.owner, "sip_ext")

        extension = Extension.objects.create(
            owner=waba_phone.owner,
            server=self.server,
            number=int(ext),
            password=password,
            date_end=date_end
        )
        waba_phone.sip_extensions = extension
        waba_phone.save()
        finish_create.delay(url, headers, phone, ext)
        return(f"Data added to FreePBX {phone, ext}")

@shared_task
def create_extension_task(phone_id):
    pbx = PbxClient()
    return pbx.create_extension(phone_id)

@shared_task
def finish_create(url, headers, phone, ext):
    gqlRoute = """
    mutation {
        addInboundRoute(
            input: {
                extension: "%s"
                destination:"from-did-direct,%s,1"
            }
        ) {
            inboundRoute {
                id
            }
            status
            message
        }
    }
    """ % (phone, ext)

    route_data = {"query": gqlRoute}
    try:
        resp_route = requests.post(url, json=route_data, headers=headers)
        resp_route.raise_for_status()
    except requests.exceptions.RequestException:
        raise Exception(phone, resp_route.json())
    
    doreload = """
    mutation {
        doreload(input: {}) {
            message
            status
            transaction_id
        }
    }
    """
    try:
        reload = requests.post(url, json={"query": doreload}, headers=headers)
        reload.raise_for_status()
        return reload.json()
    except requests.exceptions.RequestException:
        raise Exception(phone, reload.json())
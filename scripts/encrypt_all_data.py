import os
import sys
import django
from cryptography.fernet import InvalidToken

# Setup Django environment
# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

# Import all models AFTER django.setup()
from separator.freepbx.models import Server as FreepbxServer, Extension as FreepbxExtension
from separator.bitrix.models import App as BitrixApp, Credential as BitrixCredential
from separator.waba.models import App as WabaApp, Waba as WabaAccount
from separator.waweb.models import Server as WawebServer, Session as WawebSession
from separator.bitbot.models import Connector as BitbotConnector
from separator.olx.models import OlxApp, OlxUser

def run():
    # List of (Model, [fields])
    targets = [
        (FreepbxServer, ['client_id', 'client_secret']),
        (FreepbxExtension, ['password']),
        (BitrixApp, ['client_secret']),
        (BitrixCredential, ['access_token', 'refresh_token']),
        (WabaApp, ['client_secret', 'access_token']),
        (WabaAccount, ['access_token']),
        (WawebServer, ['api_key']),
        (WawebSession, ['apikey']),
        (BitbotConnector, ['key']),
        (OlxApp, ['client_secret']),
        (OlxUser, ['access_token', 'refresh_token']),
    ]

    for model, fields in targets:
        print(f"Processing {model.__name__}...")
        count = 0
        updated_count = 0
        
        # We iterate over all objects
        for obj in model.objects.all():
            count += 1
            needs_save = False
            
            for field in fields:
                try:
                    # Try to access the field. 
                    # If it's encrypted, it decrypts successfully.
                    # If it's plaintext, it MIGHT raise InvalidToken or return garbage/plaintext depending on config.
                    val = getattr(obj, field)
                    
                    # If we got here, it decrypted OR it was plaintext and the library didn't complain.
                    # To ensure it gets encrypted (or re-encrypted with new IV), we set it back.
                    setattr(obj, field, val)
                    needs_save = True
                    
                except (InvalidToken, ValueError):
                    # If accessing raises an error, it's likely plaintext that failed decryption.
                    # We need to read the RAW value from DB and set it.
                    print(f"  Found plaintext (or invalid token) in {model.__name__} ID {obj.pk} field {field}. Encrypting...")
                    
                    raw_val = list(model.objects.filter(pk=obj.pk).values_list(field, flat=True))[0]
                    setattr(obj, field, raw_val)
                    needs_save = True
                except Exception as e:
                    print(f"  Error accessing {model.__name__} ID {obj.pk} field {field}: {e}")
            
            if needs_save:
                try:
                    obj.save()
                    updated_count += 1
                except Exception as e:
                    print(f"  Error saving {model.__name__} ID {obj.pk}: {e}")

        print(f"Processed {model.__name__}: {updated_count}/{count} objects updated.")

if __name__ == "__main__":
    run()

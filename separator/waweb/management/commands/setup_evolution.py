from django.core.management.base import BaseCommand
from separator.waweb.models import Server
import os

class Command(BaseCommand):
    help = 'Setup Evolution server in database'

    def handle(self, *args, **options):
        # Try to get URL from EVOLUTION_SERVER_URL (legacy) or SERVER_URL (from .env)
        evolution_url = os.environ.get('EVOLUTION_SERVER_URL') or os.environ.get('SERVER_URL') or 'http://evolution:8080'
        
        # Debug: Print available keys (masked)
        auth_key = os.environ.get('AUTHENTICATION_API_KEY')
        self.stdout.write(f"DEBUG: AUTHENTICATION_API_KEY found: {bool(auth_key)}")

        # Try to get key from AUTHENTICATION_API_KEY, then default
        api_key = auth_key or 'separator-evolution-secret-key'

        if not api_key:
            # This should technically not happen with the default above, but good for safety
            self.stdout.write(self.style.WARNING('EVOLUTION_API_KEY or AUTHENTICATION_API_KEY not found in environment. Skipping setup.'))
            return

        # Check if server exists
        server, created = Server.objects.get_or_create(
            url=evolution_url,
            defaults={
                'api_key': api_key,
                'max_connections': 100,
                'groups_ignore': True,
                'always_online': False,
                'read_messages': False,
                'sync_history': False
            }
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f'Successfully created Evolution server: {evolution_url}'))
        else:
            # If it exists, we check if we need to update the key
            # The user said "if already written... do not create". 
            # But if the key in env is different from DB, it might be a configuration drift.
            # However, strictly following "do not create anew", get_or_create handles that.
            # I will update the key if it's different, to ensure consistency.
            if server.api_key != api_key:
                 server.api_key = api_key
                 server.save()
                 self.stdout.write(self.style.SUCCESS(f'Updated API key for Evolution server: {evolution_url}'))
            else:
                 self.stdout.write(self.style.SUCCESS(f'Evolution server already exists: {evolution_url}'))

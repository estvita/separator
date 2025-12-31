import secrets
import string
import re
import shutil
from pathlib import Path

def generate_secret_key(length=50):
    # Exclude characters that might cause issues in .env parsing if unquoted
    chars = string.ascii_letters + string.digits + "!@#$%^&*(-_=+)"
    return ''.join(secrets.choice(chars) for _ in range(length))

def generate_salt_key(length=64):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def generate_admin_url():
    return f"admin_{secrets.token_urlsafe(10)}/"

def update_env_file():
    env_path = Path('.env')
    example_path = Path('docs/example/env.example')
    
    if not env_path.exists():
        if example_path.exists():
            print("⚠️ .env file not found. Copying from docs/example/env.example...")
            shutil.copy(example_path, env_path)
        else:
            print("❌ .env file not found and docs/example/env.example is missing.")
            return

    content = env_path.read_text()
    
    # Generate keys
    secret_key = generate_secret_key()
    salt_key = generate_salt_key()
    admin_url = generate_admin_url()
    
    updated = False
    
    # Update DJANGO_SECRET_KEY
    if 'DJANGO_SECRET_KEY=' in content:
        # Check if it's already set to something other than placeholder/empty
        current_val = re.search(r'DJANGO_SECRET_KEY=(.*)', content).group(1)
        if current_val == 'CHANGE_ME' or not current_val:
            content = re.sub(r'DJANGO_SECRET_KEY=.*', f'DJANGO_SECRET_KEY={secret_key}', content)
            updated = True
            print("✅ Generated DJANGO_SECRET_KEY")
        else:
             # Force update if requested? The prompt implies generating keys. 
             # I will overwrite it to be safe as per "generate keys" request.
             content = re.sub(r'DJANGO_SECRET_KEY=.*', f'DJANGO_SECRET_KEY={secret_key}', content)
             updated = True
             print("✅ Regenerated DJANGO_SECRET_KEY")
    else:
        content += f'\nDJANGO_SECRET_KEY={secret_key}'
        updated = True
        print("✅ Added DJANGO_SECRET_KEY")
        
    # Update SALT_KEY
    if 'SALT_KEY=' in content:
        content = re.sub(r'SALT_KEY=.*', f'SALT_KEY={salt_key}', content)
        updated = True
        print("✅ Regenerated SALT_KEY")
    else:
        content += f'\nSALT_KEY={salt_key}'
        updated = True
        print("✅ Added SALT_KEY")
        
    # Update DJANGO_ADMIN_URL
    if 'DJANGO_ADMIN_URL=' in content:
        content = re.sub(r'DJANGO_ADMIN_URL=.*', f'DJANGO_ADMIN_URL={admin_url}', content)
        updated = True
        print(f"✅ Regenerated DJANGO_ADMIN_URL: {admin_url}")
    else:
        content += f'\nDJANGO_ADMIN_URL={admin_url}'
        updated = True
        print(f"✅ Added DJANGO_ADMIN_URL: {admin_url}")

    if updated:
        env_path.write_text(content)
        print("🎉 .env file successfully updated.")
    else:
        print("No changes made.")

if __name__ == "__main__":
    update_env_file()

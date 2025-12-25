import os
import secrets
import re
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def generate_key():
    return secrets.token_urlsafe(32)

def ensure_file_from_example(filename, example_path):
    filepath = os.path.join(BASE_DIR, filename)
    if not os.path.exists(filepath):
        example_full_path = os.path.join(BASE_DIR, example_path)
        if os.path.exists(example_full_path):
            print(f"Creating {filename} from {example_path}...")
            shutil.copy(example_full_path, filepath)
        else:
            print(f"Warning: Example file {example_path} not found. Creating empty {filename}.")
            open(filepath, 'w').close()
    return filepath

def update_env_key(filepath, key_name, value, default_placeholder=None):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    pattern = f"^{key_name}=.*"
    match = re.search(pattern, content, re.MULTILINE)
    
    current_value = None
    if match:
        current_value = match.group(0).split('=', 1)[1].strip()
    
    # If key exists and is not the placeholder/empty, return it
    if current_value and (not default_placeholder or current_value != default_placeholder):
        return current_value

    # Otherwise, update it
    if match:
        new_content = re.sub(pattern, f"{key_name}={value}", content, flags=re.MULTILINE)
    else:
        if content and not content.endswith('\n'):
            content += '\n'
        new_content = content + f"{key_name}={value}\n"
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    return value

def main():
    print("Initializing configuration...")
    
    # Setup .env from unified example
    env_path = ensure_file_from_example('.env', 'docs/example/env.example')
    
    new_key = generate_key()
    
    # Update AUTHENTICATION_API_KEY in .env
    update_env_key(env_path, 'AUTHENTICATION_API_KEY', new_key, 'YOUR_SECURE_TOKEN')
    
    print(f"Configuration updated in {env_path}")
    print("Done. You can now run 'docker compose up'.")

if __name__ == "__main__":
    main()

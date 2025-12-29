# Database Encryption Migration

Follow these steps to migrate the database to use encrypted fields.

1. **Update Code**
   Pull the latest changes from the repository.

2. **Stop Project**
   Stop the running application and workers to prevent data inconsistency.

3. **Run Migrations**
   Apply the database migrations to change the field types to `EncryptedCharField`.
   ```bash
   python manage.py migrate
   ```

4. **Run Encryption Script**
   Run the script to encrypt existing plaintext data in the database.
   ```bash
   python scripts/encrypt_all_data.py
   ```

5. **Start Project**
   Start the application and workers.

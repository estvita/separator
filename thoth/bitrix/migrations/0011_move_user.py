from django.db import migrations

def move_users(apps, schema_editor):
    Bitrix = apps.get_model('bitrix', 'Bitrix')
    AppInstance = apps.get_model('bitrix', 'AppInstance')
    User = apps.get_model('bitrix', 'User')
    Credential = apps.get_model('bitrix', 'Credential')

    for bitrix_obj in Bitrix.objects.all():
        user_id = getattr(bitrix_obj, 'user_id', None)
        owner = bitrix_obj.owner

        if user_id is not None:
            user = User.objects.create(
                user_id=user_id,
                admin=True,
                active=True,
                owner=owner,
                bitrix=bitrix_obj,
            )
            for appinstance in AppInstance.objects.filter(portal=bitrix_obj):
                access_token = getattr(appinstance, 'access_token', '')
                refresh_token = getattr(appinstance, 'refresh_token', '')
                Credential.objects.create(
                    user=user,
                    app_instance=appinstance,
                    access_token=access_token,
                    refresh_token=refresh_token,
                )

class Migration(migrations.Migration):

    dependencies = [
        ('bitrix', '0010_remove_adminmessage_app_instance_user_credential_and_more'),
    ]

    operations = [
        migrations.RunPython(move_users, reverse_code=migrations.RunPython.noop),
    ]
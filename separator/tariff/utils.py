from datetime import timedelta
from django.utils.timezone import now
from .models import Tariff, Trial, Service
from separator.bitrix.models import User

def get_trial(user, code: str):

    try:
        existing_trial = Trial.objects.filter(owner=user, service__code=code).first()
        if existing_trial:
            return now()
        duration_value = Tariff.objects.filter(service__code=code, is_trial=True).values_list("duration", flat=True).first()
        if not duration_value:
            return None

        service = Service.objects.filter(code=code).first()
        if not service:
            return None
        
        b24_users = User.objects.filter(owner=user).all()
        if b24_users:
            for b24_user in b24_users:
                portal = b24_user.bitrix
                portal_users = User.objects.filter(bitrix=portal).all()
                if portal_users:
                    for portal_user in portal_users:
                        existing_trial = Trial.objects.filter(owner=portal_user.owner, service__code=code).first()
                        if existing_trial:
                            return now()

        expiration_date = now() + timedelta(days=duration_value)
        Trial.objects.create(owner=user, service=service)

        return expiration_date

    except Exception as e:
        print(f"Ошибка при обработке триального периода для пользователя {user} и модуля {code}: {e}")
        return now()
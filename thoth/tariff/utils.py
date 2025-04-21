from .models import Tariff, Trial, Service
from datetime import timedelta
from django.utils.timezone import now

def get_trial(user, code: str):

    try:
        existing_trial = Trial.objects.filter(owner=user, service__code=code).first()
        if existing_trial:
            return now()
        duration_value = Tariff.objects.filter(service__code=code, is_trial=True).values_list("duration", flat=True).first()
        if not duration_value:
            return now()

        service = Service.objects.filter(code=code).first()
        if not service:
            return now()

        expiration_date = now() + timedelta(days=duration_value)
        Trial.objects.create(owner=user, service=service)

        return expiration_date

    except Exception as e:
        print(f"Ошибка при обработке триального периода для пользователя {user} и модуля {code}: {e}")
        return now()
import redis
from celery import shared_task
from celery.exceptions import Retry
from datetime import timedelta
from django.conf import settings
from django.utils import timezone

from separator.asterx.models import Server as AsterxServer
from separator.bitbot.models import ChatBot
from separator.bitrix.crest import call_method
from separator.bitrix.models import AppInstance, FeatureGrant
from separator.bitrix.retry import RETRY_KWARGS
from separator.olx.models import OlxUser
from separator.users.models import Message, User
from separator.voip.models import Extension
from separator.waba.models import Phone
from separator.waweb.models import Session


redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)


def build_lead_title(site, code, fallback, **context):
    template = Message.objects.filter(sites=site, code=code).first() if site else None
    text = template.message if template and template.message else fallback
    try:
        return text.format(**context)
    except Exception:
        return fallback.format(**context)


@shared_task(queue='bitrix', **RETRY_KWARGS)
def prepare_lead(user_id, lead_title):
    user = User.objects.filter(id=user_id).first()
    if not user:
        raise Exception("user not found")
    email = (user.email or "").strip()
    if email.lower().startswith("bitrix-") and email.lower().endswith("@local"):
        email = ""
    phone = str(user.phone_number) if user.phone_number else ""
    if not email and not phone:
        return

    site = user.site
    if not site:
        raise Exception(f"site for {user.email} not found")
    if not site.profile:
        raise Exception(f"site.profile for {site.domain} not found")
    owner = site.profile.owner
    vendor_instance = AppInstance.objects.filter(owner=owner, app__vendor=True).first()
    if not vendor_instance:
        raise Exception(f"vendor instance for vendor {owner.email} not found")
    if email and phone:
        client_filter = {
            "logic": "OR",
            "0": {"phone": phone},
            "1": {"email": email}
        }
    elif phone:
        client_filter = {"phone": phone}
    else:
        client_filter = {"email": email}
    payload = {
        "filter": {
            "0": client_filter,
        },
        "entityTypeId": 3, #contacts
    }
    client_data = call_method(vendor_instance, "crm.item.list", payload)
    contact_ids = []
    if "result" in client_data:
        client_data = client_data.get("result", {}).get("items", [])
        contact_ids = [contact.get("id") for contact in client_data if contact.get("id")]

    lead_data = {
        "fields": {
            "TITLE": lead_title,
        }
    }
    if contact_ids:
        lead_data["fields"]["CONTACT_IDS"] = contact_ids
    else:
        lead_data["fields"]["NAME"] = user.name
        if email:
            lead_data["fields"]["EMAIL"] = [{
                "VALUE": email,
                "VALUE_TYPE": "WORK"
            }]
        if phone:
            lead_data["fields"]["PHONE"] = [{
                "VALUE": phone,
                "VALUE_TYPE": "MOBILE"
            }]

    return call_method(vendor_instance, "crm.lead.add", lead_data)


@shared_task(queue='bitrix', **RETRY_KWARGS)
def check_tariffs(*days, entities=None):
    """
    Check expiration for service subscriptions and feature grants.
    Accepts multiple integer arguments and optional entity codes.
    Example: check_tariffs(10, 5, 1, entities=['waba', 'feature:call_management'])
    """
    days_list = [d for d in days if isinstance(d, int)]

    if not days_list:
        return

    check_all = not entities
    if isinstance(entities, str):
        entity_codes = {entities}
    else:
        entity_codes = set(entities or [])
    feature_codes = {
        entity_code.removeprefix('feature:')
        for entity_code in entity_codes
        if entity_code.startswith('feature:') and entity_code != 'feature:'
    }

    today = timezone.now().date()
    max_days = max(days_list) if days_list else 1
    ttl = max_days * 24 * 60 * 60

    mapping = [
        (Phone, 'date_end', 'phone', 'waba'),
        (Session, 'date_end', 'phone', 'waweb'),
        (OlxUser, 'date_end', 'olx_id', 'olx'),
        (ChatBot, 'date_end', 'name', 'bitbot'),
        (AsterxServer, 'date_end', 'name', 'asterx'),
        (Extension, 'date_end', 'number', 'wa_calls'),
    ]

    for days in days_list:
        target_date = today + timedelta(days=days)

        for Model, date_field, id_field, service in mapping:
            if not check_all and service not in entity_codes:
                continue

            filter_kwargs = {
                f"{date_field}__year": target_date.year,
                f"{date_field}__month": target_date.month,
                f"{date_field}__day": target_date.day,
            }

            for record in Model.objects.filter(**filter_kwargs):
                if not record.owner:
                    continue

                redis_key = f"leads:{service}:{record.id}"
                if redis_client.get(redis_key):
                    continue

                identifier = getattr(record, id_field, 'Unknown')
                if Model is Extension:
                    phone = Phone.objects.filter(sip_extensions=record).first()
                    if phone:
                        identifier = phone.phone
                expiration_str = record.date_end.strftime('%d.%m.%Y')
                title = build_lead_title(
                    record.owner.site if record.owner else None,
                    "service_subscription_title",
                    "Subscription for {service}: {identifier} expires on {expiration_date}",
                    service=service,
                    identifier=identifier,
                    expiration_date=expiration_str,
                )

                try:
                    resp = prepare_lead(record.owner.id, title)
                    if resp and isinstance(resp, dict) and 'result' in resp:
                        redis_client.setex(redis_key, ttl, resp['result'])
                except Retry:
                    raise
                except Exception as e:
                    print(e)

        feature_grants = FeatureGrant.objects.none()
        if check_all or feature_codes:
            feature_grants = FeatureGrant.objects.filter(
                date_end__year=target_date.year,
                date_end__month=target_date.month,
                date_end__day=target_date.day,
                portal__owner__isnull=False,
            )
            if not check_all:
                feature_grants = feature_grants.filter(feature__code__in=feature_codes)
            feature_grants = feature_grants.select_related("feature", "portal", "portal__owner")

        for grant in feature_grants:
            owner = grant.portal.owner
            redis_key = f"leads:feature:{grant.id}"
            if redis_client.get(redis_key):
                continue

            feature_name = grant.feature.name
            expiration_str = grant.date_end.strftime('%d.%m.%Y')
            title = build_lead_title(
                owner.site,
                "service_subscription_title",
                "Subscription for {service}: expires on {expiration_date}",
                service=feature_name,
                expiration_date=expiration_str,
            )

            try:
                resp = prepare_lead(owner.id, title)
                if resp and isinstance(resp, dict) and 'result' in resp:
                    redis_client.setex(redis_key, ttl, resp['result'])
            except Retry:
                raise
            except Exception as e:
                print(e)

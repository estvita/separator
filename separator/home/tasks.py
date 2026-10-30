import base64
import hashlib
import imaplib
import logging
import redis
import re
from celery import Task, shared_task
from celery.exceptions import Retry
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from io import BytesIO
from django.conf import settings
from django.utils import timezone
from pypdf import PdfReader

from separator.asterx.models import Server as AsterxServer
from separator.bitbot.models import ChatBot
from separator.bitrix.crest import BitrixAccessDeniedError, call_method
from separator.bitrix.models import AppInstance, FeatureGrant
from separator.bitrix.retry import RETRY_KWARGS
from separator.olx.models import OlxUser
from separator.users.models import Message, User
from separator.voip.models import Extension
from separator.waba.models import Phone
from separator.waweb.models import Session


redis_client = redis.StrictRedis.from_url(settings.REDIS_URL)
logger = logging.getLogger("django")


class HiddenTaskArguments(Task):
    abstract = True

    def apply_async(self, args=None, kwargs=None, **options):
        options["argsrepr"] = "<hidden>"
        options["kwargsrepr"] = "<hidden>"
        return super().apply_async(args=args, kwargs=kwargs, **options)


def _imap_date(value):
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return f"{value.day:02d}-{months[value.month - 1]}-{value.year}"


def _imap_mailbox_name(mailbox):
    escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _fetch_imap_literal(connection, uid, query):
    status, response = connection.uid("fetch", uid, query)
    if status != "OK":
        raise RuntimeError(f"Gmail fetch failed for UID {uid!r}: {status}")

    metadata = b""
    content = None
    for item in response or []:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        metadata = item[0] or b""
        content = item[1]
        break

    if content is None:
        raise RuntimeError(f"Gmail returned no message content for UID {uid!r}")
    return metadata, content


def _gmail_message_header(connection, uid):
    metadata, content = _fetch_imap_literal(
        connection,
        uid,
        "(X-GM-MSGID BODY.PEEK[HEADER.FIELDS (SUBJECT)])",
    )
    match = re.search(rb"X-GM-MSGID\s+(\d+)", metadata)
    if not match:
        raise RuntimeError(f"Gmail returned no X-GM-MSGID for UID {uid!r}")
    message = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
    return match.group(1).decode("ascii"), message


def _pdf_attachments(message):
    files = []
    parts = [message]
    while parts:
        part = parts.pop()
        if part.get_content_maintype() == "message":
            continue
        if part.is_multipart():
            parts.extend(reversed(list(part.iter_parts())))
            continue

        filename = part.get_filename()
        if part.get_content_disposition() != "attachment" and not filename:
            continue

        content_type = part.get_content_type().lower()
        filename = str(filename or "document.pdf")
        if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue

        content = part.get_payload(decode=True)
        if not content or b"%PDF-" not in content[:1024]:
            continue

        filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        filename = filename or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        files.append((filename, content))

    return files


def _extract_mask_value(text, mask):
    if not mask:
        return None
    match = re.search(rf"{re.escape(mask)}[A-Za-z0-9-]+", text)
    return match.group(0) if match else None


def _extract_pdf_value(files, mask):
    if not mask:
        return None

    for _, content in files:
        reader = PdfReader(BytesIO(content))
        for page in reader.pages:
            value = _extract_mask_value(page.extract_text() or "", mask)
            if value:
                return value

    return None


def _message_text(message):
    parts = []
    for part in message.walk():
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        content = part.get_content()
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def _normalized_text(value):
    return value.lower().replace("ё", "е")


def _notify_bitrix_license_expired(app_instance):
    if not app_instance.owner_id:
        return
    title = build_lead_title(
        app_instance.owner.site,
        "bitrix_license_title",
        "License expired for portal {portal}",
        portal=app_instance.portal.domain,
    )
    try:
        prepare_lead.delay(app_instance.owner_id, title)
    except Exception:
        logger.exception(
            "Failed to enqueue Bitrix license notification for app instance %s",
            app_instance.id,
        )


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


@shared_task(queue="bitrix", **RETRY_KWARGS)
def update_bitrix_deal(
    app_instance_id,
    deal_id,
    file_field,
    files,
    processed_key,
    processed_ttl,
    clear_file_field=1,
    pdf_value_field="",
    pdf_value=None,
):
    clear_file_field = int(clear_file_field)
    if clear_file_field not in (0, 1):
        raise ValueError("clear_file_field must be 0 or 1")

    if redis_client.exists(processed_key):
        return {
            "status": "skipped",
            "reason": "already_sent",
            "deal_id": deal_id,
        }

    app_instance = AppInstance.objects.select_related("owner", "owner__site", "portal").get(
        id=app_instance_id,
    )
    try:
        field_value = list(files)
        if not clear_file_field:
            get_response = call_method(
                app_instance,
                "crm.item.get",
                {
                    "entityTypeId": 2,
                    "id": deal_id,
                    "useOriginalUfNames": "Y",
                },
            )
            item = (
                get_response.get("result", {}).get("item")
                if isinstance(get_response, dict)
                else None
            )
            if not isinstance(item, dict):
                raise Exception({
                    "message": "Failed to get Bitrix deal",
                    "deal_id": deal_id,
                    "response": get_response,
                })

            current_files = item.get(file_field) or []
            if not isinstance(current_files, list):
                raise Exception({
                    "message": "Unexpected Bitrix multiple file field value",
                    "deal_id": deal_id,
                    "field": file_field,
                    "value": current_files,
                })

            existing_files = []
            for current_file in current_files:
                if not isinstance(current_file, dict) or not current_file.get("id"):
                    raise Exception({
                        "message": "Unexpected Bitrix file value",
                        "deal_id": deal_id,
                        "field": file_field,
                        "value": current_file,
                    })
                existing_files.append({"id": current_file["id"]})
            field_value = existing_files + field_value

        fields = {file_field: field_value}
        if pdf_value_field and pdf_value:
            fields[pdf_value_field] = pdf_value

        response = call_method(
            app_instance,
            "crm.item.update",
            {
                "entityTypeId": 2,
                "id": deal_id,
                "useOriginalUfNames": "Y",
                "fields": fields,
            },
        )
    except BitrixAccessDeniedError:
        _notify_bitrix_license_expired(app_instance)
        return {
            "status": "skipped",
            "reason": "license_expired",
            "deal_id": deal_id,
        }

    result = response.get("result", {}) if isinstance(response, dict) else {}
    if not isinstance(result, dict) or not result.get("item"):
        raise Exception({
            "message": "Bitrix deal update failed",
            "deal_id": deal_id,
            "response": response,
        })

    if pdf_value:
        redis_client.setex(f"gmail:{pdf_value}", processed_ttl, deal_id)
    redis_client.setex(processed_key, processed_ttl, deal_id)
    return response


@shared_task(queue="bitrix", **RETRY_KWARGS)
def confirm_bitrix_deal(
    app_instance_id,
    doc_number,
    act_field,
    confirmation_field,
    processed_ttl,
):
    confirmed_key = f"gmail:confirmed:{doc_number}"
    if redis_client.exists(confirmed_key):
        return {
            "status": "skipped",
            "reason": "already_confirmed",
            "doc_number": doc_number,
        }

    app_instance = AppInstance.objects.select_related("owner", "owner__site", "portal").get(
        id=app_instance_id,
    )
    try:
        deal_id = redis_client.get(f"gmail:{doc_number}")
        if deal_id is not None:
            deal_id = int(deal_id)
        else:
            search_response = call_method(
                app_instance,
                "crm.item.list",
                {
                    "entityTypeId": 2,
                    "filter": {act_field: doc_number},
                    "select": ["id"],
                    "useOriginalUfNames": "Y",
                },
            )
            items = (
                search_response.get("result", {}).get("items")
                if isinstance(search_response, dict)
                else None
            )
            if not isinstance(items, list) or len(items) != 1:
                return search_response
            deal_id = int(items[0]["id"])

        response = call_method(
            app_instance,
            "crm.item.update",
            {
                "entityTypeId": 2,
                "id": deal_id,
                "useOriginalUfNames": "Y",
                "fields": {confirmation_field: "Y"},
            },
        )
    except BitrixAccessDeniedError:
        _notify_bitrix_license_expired(app_instance)
        return {
            "status": "skipped",
            "reason": "license_expired",
            "doc_number": doc_number,
        }

    result = response.get("result", {}) if isinstance(response, dict) else {}
    if not isinstance(result, dict) or not result.get("item"):
        raise Exception({
            "message": "Bitrix deal confirmation failed",
            "deal_id": deal_id,
            "doc_number": doc_number,
            "response": response,
        })

    redis_client.setex(f"gmail:{doc_number}", processed_ttl, deal_id)
    redis_client.setex(confirmed_key, processed_ttl, 1)
    return response


@shared_task(queue="bitrix", base=HiddenTaskArguments)
def check_gmail_messages(
    imap_host,
    imap_username,
    imap_password,
    app_instance_id,
    file_field,
    imap_port=993,
    mailbox="INBOX",
    subject_pattern=r"#D_(\d+)",
    lookback_days=30,
    clear_file_field=1,
    pdf_value_mask="",
    pdf_value_field="",
    confirmation_mailbox="",
    confirmation_body_mask="",
    confirmation_field="",
):
    lookback_days = int(lookback_days)
    clear_file_field = int(clear_file_field)
    imap_port = int(imap_port)
    pdf_value_mask = (pdf_value_mask or "").strip()
    pdf_value_field = (pdf_value_field or "").strip()
    confirmation_mailbox = (confirmation_mailbox or "").strip()
    confirmation_body_mask = (confirmation_body_mask or "").strip()
    confirmation_field = (confirmation_field or "").strip()
    confirmation_enabled = any((
        confirmation_mailbox,
        confirmation_body_mask,
        confirmation_field,
    ))

    if lookback_days < 1:
        raise ValueError("lookback_days must be greater than zero")
    if clear_file_field not in (0, 1):
        raise ValueError("clear_file_field must be 0 or 1")
    if not file_field.startswith("UF_CRM_"):
        raise ValueError("file_field must be a Bitrix UF_CRM field")
    if pdf_value_field and not pdf_value_field.startswith("UF_CRM_"):
        raise ValueError("pdf_value_field must be a Bitrix UF_CRM field")
    if pdf_value_mask and not pdf_value_field:
        raise ValueError("pdf_value_field is required when pdf_value_mask is set")
    if confirmation_enabled and not all((
        confirmation_mailbox,
        confirmation_body_mask,
        confirmation_field,
    )):
        raise ValueError("all confirmation parameters must be set")
    if confirmation_enabled and not pdf_value_mask:
        raise ValueError("pdf_value_mask is required for confirmation checks")
    if confirmation_field and not confirmation_field.startswith("UF_CRM_"):
        raise ValueError("confirmation_field must be a Bitrix UF_CRM field")

    deal_pattern = re.compile(subject_pattern)
    if deal_pattern.groups < 1:
        raise ValueError("subject_pattern must contain a deal ID capture group")

    account_hash = hashlib.sha256(
        f"{imap_host}|{imap_username.lower()}|{mailbox}".encode("utf-8"),
    ).hexdigest()[:24]
    processed_ttl = (lookback_days + 1) * 24 * 60 * 60
    since = timezone.now().date() - timedelta(days=lookback_days)
    incoming_search = (
        "SINCE",
        _imap_date(since),
        "NOT", "X-GM-LABELS", r'"\\Sent"',
        "NOT", "X-GM-LABELS", r'"\\Drafts"',
    )
    stats = {
        "found": 0,
        "scheduled": 0,
        "already_sent": 0,
        "skipped": 0,
        "failed": 0,
        "confirmation_found": 0,
        "confirmation_scheduled": 0,
        "confirmation_already_sent": 0,
        "confirmation_skipped": 0,
        "confirmation_failed": 0,
    }

    connection = None
    try:
        connection = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=30)
        connection.login(imap_username, imap_password)
        status, _ = connection.select(_imap_mailbox_name(mailbox), readonly=True)
        if status != "OK":
            raise RuntimeError(f"Gmail label not found: {mailbox}")

        status, response = connection.uid("search", None, *incoming_search)
        if status != "OK":
            raise RuntimeError(f"Gmail search failed: {status}")

        uids = response[0].split() if response and response[0] else []
        stats["found"] = len(uids)

        for uid in uids:
            try:
                gmail_message_id, header = _gmail_message_header(connection, uid)
            except Exception:
                stats["failed"] += 1
                logger.exception("Failed to read Gmail message header for UID %r", uid)
                continue

            processed_key = f"gmail:sent:{account_hash}:{gmail_message_id}"
            if redis_client.exists(processed_key):
                stats["already_sent"] += 1
                continue

            subject = str(header.get("Subject", ""))
            deal_match = deal_pattern.search(subject)
            if not deal_match:
                stats["skipped"] += 1
                continue

            try:
                deal_id = int(deal_match.group(1))
            except (TypeError, ValueError):
                stats["skipped"] += 1
                continue

            try:
                _, raw_message = _fetch_imap_literal(
                    connection,
                    uid,
                    "(BODY.PEEK[])",
                )
                message = BytesParser(policy=policy.default).parsebytes(raw_message)
                attachments = _pdf_attachments(message)
                if not attachments:
                    stats["skipped"] += 1
                    continue

                pdf_value = _extract_pdf_value(attachments, pdf_value_mask)

                update_bitrix_deal.delay(
                    app_instance_id,
                    deal_id,
                    file_field,
                    [
                        [
                            filename,
                            base64.b64encode(file_content).decode("ascii"),
                        ]
                        for filename, file_content in attachments
                    ],
                    processed_key,
                    processed_ttl,
                    clear_file_field,
                    pdf_value_field,
                    pdf_value,
                )
                stats["scheduled"] += 1
            except Exception:
                stats["failed"] += 1
                logger.exception(
                    "Failed to process Gmail message UID %r for Bitrix deal %s",
                    uid,
                    deal_id,
                )

        if confirmation_enabled:
            status, _ = connection.select(
                _imap_mailbox_name(confirmation_mailbox),
                readonly=True,
            )
            if status != "OK":
                raise RuntimeError(
                    f"Gmail confirmation label not found: {confirmation_mailbox}",
                )

            status, response = connection.uid(
                "search",
                None,
                *incoming_search,
            )
            if status != "OK":
                raise RuntimeError(f"Gmail confirmation search failed: {status}")

            confirmation_uids = (
                response[0].split() if response and response[0] else []
            )
            stats["confirmation_found"] = len(confirmation_uids)
            scheduled_acts = set()

            for uid in confirmation_uids:
                try:
                    _, raw_message = _fetch_imap_literal(
                        connection,
                        uid,
                        "(BODY.PEEK[])",
                    )
                    message = BytesParser(policy=policy.default).parsebytes(raw_message)
                    body = _message_text(message)
                    if (
                        _normalized_text(confirmation_body_mask)
                        not in _normalized_text(body)
                    ):
                        stats["confirmation_skipped"] += 1
                        continue

                    doc_number = _extract_mask_value(body, pdf_value_mask)
                    if not doc_number:
                        stats["confirmation_skipped"] += 1
                        continue

                    confirmed_key = f"gmail:confirmed:{doc_number}"
                    if (
                        doc_number in scheduled_acts
                        or redis_client.exists(confirmed_key)
                    ):
                        stats["confirmation_already_sent"] += 1
                        continue

                    confirm_bitrix_deal.delay(
                        app_instance_id,
                        doc_number,
                        pdf_value_field,
                        confirmation_field,
                        processed_ttl,
                    )
                    scheduled_acts.add(doc_number)
                    stats["confirmation_scheduled"] += 1
                except Exception:
                    stats["confirmation_failed"] += 1
                    logger.exception(
                        "Failed to process Gmail confirmation UID %r",
                        uid,
                    )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
            try:
                connection.logout()
            except Exception:
                pass

    return stats


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

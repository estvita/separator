import argparse
import os
import sys

import django


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)


def setup_django(settings_module: str):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    django.setup()


def resolve_subscribed_status(response_data, app_client_id: str) -> bool:
    data = response_data.get("data")
    if not isinstance(data, list):
        return False

    for item in data:
        app_data = item.get("whatsapp_business_api_data", {})
        if str(app_data.get("id")) == str(app_client_id):
            return True

        # Fallback for possible alternative response shapes
        if str(item.get("id")) == str(app_client_id):
            return True

    return False


def run(dry_run: bool = False, check: bool = False):
    from separator.waba.models import Waba
    from separator.waba.utils import call_api

    total = 0
    updated = 0
    skipped = 0
    errors = 0

    queryset = Waba.objects.select_related("app").all().order_by("id")

    for waba in queryset:
        total += 1

        if not waba.app:
            skipped += 1
            print(f"SKIP waba_id={waba.waba_id}: app is not set")
            continue

        try:
            response = call_api(app=waba.app, endpoint=f"{waba.waba_id}/subscribed_apps", method="get")
            facebook_status = resolve_subscribed_status(response, waba.app.client_id)

            if facebook_status != waba.subscribed:
                db_action = f"set subscribed={facebook_status}"
            else:
                db_action = "no changes"

            if check:
                print(
                    "CHECK "
                    f"waba_id={waba.waba_id} "
                    f"facebook_subscribed={facebook_status} "
                    f"db_subscribed={waba.subscribed} "
                    f"action={db_action}"
                )
                if facebook_status != waba.subscribed:
                    updated += 1
                continue

            if facebook_status != waba.subscribed:
                if dry_run:
                    print(
                        f"DRY-RUN waba_id={waba.waba_id}: subscribed {waba.subscribed} -> {facebook_status}"
                    )
                else:
                    Waba.objects.filter(pk=waba.pk).update(subscribed=facebook_status)
                    print(f"UPDATED waba_id={waba.waba_id}: subscribed {waba.subscribed} -> {facebook_status}")
                updated += 1
            else:
                print(f"OK waba_id={waba.waba_id}: subscribed={waba.subscribed}")

        except Exception as exc:
            errors += 1
            print(f"ERROR waba_id={waba.waba_id}: {exc}")

    print("-")
    print(f"Total: {total}")
    print(f"Updated: {updated}")
    print(f"Skipped: {skipped}")
    print(f"Errors: {errors}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync Waba.subscribed with current Facebook subscribed_apps status."
    )
    parser.add_argument(
        "--settings",
        default="config.settings.production",
        help="Django settings module (default: config.settings.production)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without writing to database",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Health-check mode: print Facebook status and planned DB action without writing",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_django(args.settings)
    run(dry_run=args.dry_run, check=args.check)

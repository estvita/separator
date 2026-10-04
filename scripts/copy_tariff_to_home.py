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


def run():
    from django.core.management.color import no_style
    from django.db import connection, transaction

    from separator.home.models import Service, Tariff, Trial
    from separator.tariff.models import (
        Service as SourceService,
        Tariff as SourceTariff,
        Trial as SourceTrial,
    )

    with transaction.atomic():
        service_count = 0
        tariff_count = 0
        trial_count = 0

        for source in SourceService.objects.all().iterator():
            Service.objects.update_or_create(
                pk=source.pk,
                defaults={
                    "name": source.name,
                    "code": source.code,
                },
            )
            service_count += 1

        for source in SourceTariff.objects.all().iterator():
            Tariff.objects.update_or_create(
                pk=source.pk,
                defaults={
                    "site_id": source.site_id,
                    "service_id": source.service_id,
                    "is_trial": source.is_trial,
                    "self_hosted": source.self_hosted,
                    "price": source.price,
                    "currency": source.currency,
                    "duration": source.duration,
                    "period": source.period,
                    "description": source.description,
                },
            )
            tariff_count += 1

        for source in SourceTrial.objects.all().iterator():
            Trial.objects.update_or_create(
                pk=source.pk,
                defaults={
                    "owner_id": source.owner_id,
                    "service_id": source.service_id,
                },
            )
            trial_count += 1

        sequence_sql = connection.ops.sequence_reset_sql(
            no_style(),
            [Service, Tariff, Trial],
        )
        with connection.cursor() as cursor:
            for sql in sequence_sql:
                cursor.execute(sql)

    print(
        "Copied "
        f"{service_count} services, "
        f"{tariff_count} tariffs and "
        f"{trial_count} trials."
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy services, tariffs and trials from tariff to home."
    )
    parser.add_argument(
        "--settings",
        default="config.settings.production",
        help="Django settings module (default: config.settings.production)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_django(args.settings)
    run()

#!/usr/bin/env python3
import os
import sys


def setup_django():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, base_dir)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
    import django  # noqa: WPS433
    django.setup()


def main():
    setup_django()
    from separator.waba.models import Waba, Template  # noqa: WPS433
    from separator.waba import utils  # noqa: WPS433

    wabas = Waba.objects.all().order_by("id")
    for waba in wabas:
        Template.objects.filter(waba=waba).delete()
        utils.save_approved_templates(waba.id)
        print(f"Refreshed templates for WABA {waba.waba_id}")


if __name__ == "__main__":
    main()

"""Create the PM Demand dashboard's permission row, and nothing else.

Django normally creates a custom permission through the ``post_migrate``
signal, which means running ``migrate``. On this project the default database
is the live one, so a bare ``migrate`` would also apply whatever other apps
happen to have pending -- see the team's standing rule about it. This command
does the one write the feature needs and no other: it asks Django's own
``create_permissions`` to reconcile just ``pm_demand``.

Idempotent. It creates one ``ContentType`` row and one ``Permission`` row the
first time, and does nothing on every run after that. The sentinel model is
``managed = False``, so no table is created either way.

    python manage.py sync_pm_demand_permission
    python manage.py sync_pm_demand_permission --dry-run

Granting the permission to a group is deliberately left out: who may read the
packaging spend is a business decision, not a deployment step.
"""

from django.apps import apps as django_apps
from django.contrib.auth.management import create_permissions
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand

APP_LABEL = "pm_demand"
CODENAME = "can_view_pm_demand"


class Command(BaseCommand):
    help = "Create the pm_demand.can_view_pm_demand permission (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report whether the permission exists without writing anything.",
        )

    def handle(self, *args, **options):
        app_config = django_apps.get_app_config(APP_LABEL)

        existed = Permission.objects.filter(
            content_type__app_label=APP_LABEL, codename=CODENAME
        ).exists()

        if options["dry_run"]:
            self.stdout.write(
                f"{APP_LABEL}.{CODENAME}: "
                + ("already present, nothing to do" if existed else "MISSING, would be created")
            )
            return

        if existed:
            self.stdout.write(
                self.style.SUCCESS(f"{APP_LABEL}.{CODENAME} already present.")
            )
            return

        # verbosity 0 so Django's own chatter does not bury the one line that
        # matters; interactive=False because this must be safe from a deploy.
        create_permissions(app_config, verbosity=0, interactive=False)

        if Permission.objects.filter(
            content_type__app_label=APP_LABEL, codename=CODENAME
        ).exists():
            self.stdout.write(self.style.SUCCESS(f"Created {APP_LABEL}.{CODENAME}."))
        else:
            self.stderr.write(
                self.style.ERROR(
                    f"{APP_LABEL}.{CODENAME} still missing. Is '{APP_LABEL}' in "
                    "INSTALLED_APPS, and does its sentinel model still declare "
                    "the permission?"
                )
            )

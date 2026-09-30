"""Take the SAP copies that are due, then exit.

Run by a systemd timer every 15 minutes (see ``sap_mirror/deploy/``). Each list
says when it is due -- the master lists once a night -- so most runs find
nothing to do and cost a second. A run while HANA is down changes nothing: the
last good copy stays, and the next run tries again.

``--force`` takes every copy now, due or not.
"""

from django.core.management.base import BaseCommand

from sap_mirror import services


class Command(BaseCommand):
    help = "Refresh the SAP copies the app falls back on when HANA is down."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Refresh every copy now.")

    def handle(self, *args, force=False, **options):
        for state in services.sync_due(force=force):
            outcome = state.last_error or f"{state.row_count} rows"
            self.stdout.write(f"{state.company.code} {state.name}: {outcome}")

"""Delete API log rows older than ``API_LOG_RETENTION_DAYS``, then exit.

Run nightly by a systemd timer (see ``api_log/deploy/``). Deletes a batch at a
time, so a long backlog never holds one huge lock on a table every call writes
to.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from api_log.models import ApiCall

BATCH = 10_000


class Command(BaseCommand):
    help = "Delete API call log rows older than the retention."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=None,
            help="Keep this many days instead of API_LOG_RETENTION_DAYS.",
        )

    def handle(self, *args, days=None, **options):
        days = days if days is not None else settings.API_LOG_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=days)
        deleted = 0
        while True:
            ids = list(
                ApiCall.objects.filter(started_at__lt=cutoff).values_list("id", flat=True)[:BATCH]
            )
            if not ids:
                break
            deleted += ApiCall.objects.filter(id__in=ids).delete()[0]
        self.stdout.write(f"Deleted {deleted} API calls from before {cutoff:%Y-%m-%d %H:%M}.")

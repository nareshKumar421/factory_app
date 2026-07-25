"""Remove abandoned GRPO drafts and their orphaned attachment files.

A GRPO saved via the save-then-post flow lives as a ``GRPOPosting`` with
``status=DRAFT`` until it is posted (on success the draft row is removed).
A draft that is never posted lingers, and its attachment files sit on disk.
This command prunes DRAFT postings older than ``--days`` (default 30) that
were never posted, deleting their files first so nothing is orphaned.

FAILED postings are deliberately left alone -- they are the retry backlog and
carry the payload/attachments an operator needs to fix and re-post.

    python manage.py cleanup_grpo_drafts --days 30
    python manage.py cleanup_grpo_drafts --days 30 --dry-run
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from grpo.models import GRPOPosting, GRPOStatus


class Command(BaseCommand):
    help = "Delete abandoned (unposted) GRPO drafts older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete DRAFT postings whose last update is older than this "
                 "many days (default: 30).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)

        drafts = (
            GRPOPosting.objects.filter(
                status=GRPOStatus.DRAFT,
                updated_at__lt=cutoff,
            )
            .prefetch_related("attachments")
        )

        count = drafts.count()
        if not count:
            self.stdout.write(
                f"No abandoned GRPO drafts older than {days} day(s)."
            )
            return

        file_count = 0
        for draft in drafts:
            for att in draft.attachments.all():
                file_count += 1
                if not dry_run and att.file:
                    att.file.delete(save=False)
            if not dry_run:
                draft.delete()

        verb = "Would delete" if dry_run else "Deleted"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {count} abandoned GRPO draft(s) "
                f"(older than {days} day(s)) and {file_count} attachment file(s)."
            )
        )

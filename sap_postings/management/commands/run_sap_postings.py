"""The SAP posting worker: sends what SAP was down for, once it answers.

Runs for good under systemd (see ``sap_postings/deploy/``). Each pass:

1. keeps the SAP health check fresh -- a probe every 30s whether or not anyone
   has the app open -- and is the one process that sends the SAP-down alerts;
2. on SAP coming back (or being up when it starts), brings every waiting
   posting forward to now;
3. sends what is due (``services.run_due``), oldest first;
4. every few minutes, checks that the app's copies of SAP (``sap_mirror``) are
   still being refreshed, and alerts the same people if one has stopped.

``--exit-when-moved <symlink>``: stop when the release that symlink points at is
no longer the one this process runs from. systemd starts it again from the new
release, so a deploy never leaves the worker sending with yesterday's code.

``--require-test-sap``: refuse to start unless every company database is a
``TEST_`` copy. For a worker on a developer's machine: its app database is full
of test postings, and one started after ``.env`` went back to live SAP would
send them all there. Such a worker does not watch the copies either: nothing
refreshes them on a schedule there, so it would only cry wolf.
"""

import logging
import os
import time

#: Seconds between checks that the SAP copies are still being refreshed.
COPY_WATCH_INTERVAL = 300
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from sap_client import health
from sap_postings import services

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send queued SAP postings as SAP answers, and keep the SAP health check fresh."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="One pass, then exit.")
        parser.add_argument("--interval", type=float, default=10.0, help="Seconds between passes.")
        parser.add_argument(
            "--exit-when-moved",
            default=os.environ.get("SAP_POSTINGS_RELEASE_LINK", ""),
            help="A symlink to the live release; exit when it points elsewhere.",
        )
        parser.add_argument(
            "--require-test-sap",
            action="store_true",
            help="Refuse to run unless every COMPANY_DB is a TEST_ copy (developer machines).",
        )

    def handle(
        self, *args, once=False, interval=10.0, exit_when_moved="", require_test_sap=False,
        **options,
    ):
        if require_test_sap:
            live = sorted(db for db in settings.COMPANY_DB.values() if not db.upper().startswith("TEST_"))
            if live:
                raise CommandError(
                    f"Refusing to send SAP postings from here: {', '.join(live)} is live SAP, "
                    f"and --require-test-sap allows only TEST_ company databases."
                )
        running_from = Path(settings.BASE_DIR).resolve()
        self.watch_copies = not require_test_sap
        self.copies_checked_at = 0.0
        was_up = None
        self.stdout.write(f"SAP posting worker started from {running_from}")
        while True:
            close_old_connections()
            try:
                was_up = self.one_pass(was_up)
            except Exception:  # noqa: BLE001 -- a bad pass must not end the worker
                logger.exception("SAP posting worker pass failed")
            close_old_connections()
            if once:
                return
            if exit_when_moved and Path(exit_when_moved).resolve() != running_from:
                self.stdout.write("The live release moved; exiting for systemd to restart me.")
                return
            time.sleep(interval)

    def one_pass(self, was_up):
        # The worker is the one process that alerts; see sap_client.health.
        snap = health.snapshot(alert=True)
        is_up = snap["components"][health.SERVICE_LAYER]["status"] == health.UP
        # Up after being down -- or up when the worker starts, which may be
        # right after SAP came back: nothing waiting should sit out its backoff.
        if is_up and was_up is not True:
            moved = services.bring_forward()
            if moved:
                logger.warning("SAP Service Layer is up; %s waiting posting(s) sent now", moved)
        sent = services.run_due()
        if sent:
            logger.info("Sent %s SAP posting(s)", sent)
        if getattr(self, "watch_copies", False) and time.time() - self.copies_checked_at >= COPY_WATCH_INTERVAL:
            self.copies_checked_at = time.time()
            try:
                from sap_mirror import monitor

                monitor.watch(snap)
            except Exception:  # noqa: BLE001 -- the copies are never why postings stop
                logger.exception("Checking the SAP copies failed")
        return is_up

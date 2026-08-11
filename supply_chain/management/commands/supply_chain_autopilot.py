"""Run the Live Trail autopilot by hand.

The scheduler runs this every morning; this command exists so it can be proved
in production without waiting for 07:00 and without paging anyone:

    python manage.py supply_chain_autopilot --dry-run
    python manage.py supply_chain_autopilot --force
"""
from django.core.management.base import BaseCommand

from supply_chain.services.live_trail import PRODUCTION_COMPANY
from supply_chain.services.live_trail_autopilot import run_live_trail_autopilot


class Command(BaseCommand):
    help = "Read the Live Trail and send each department the actions it owns."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=PRODUCTION_COMPANY)
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Build every digest and send nothing.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-send a digest that has not changed since the last one.",
        )

    def handle(self, *args, **options):
        results = run_live_trail_autopilot(
            options["company"],
            force=options["force"],
            dry_run=options["dry_run"],
        )

        for result in results:
            if result.get("sent"):
                self.stdout.write(self.style.SUCCESS(
                    f"SENT  {result['department']}: {result['actions']} action(s) "
                    f"to {result['recipients']} recipient(s) — {result['title']}"
                ))
            elif options["dry_run"] and result.get("title"):
                self.stdout.write(self.style.WARNING(
                    f"DRY   {result['department']} -> {result['permission']}\n"
                    f"      {result['title']}\n"
                    + "\n".join(f"      {line}" for line in result["body"].splitlines())
                ))
            else:
                self.stdout.write(
                    f"QUIET {result['department']}: {result['reason']}"
                )

        sent = sum(1 for r in results if r.get("sent"))
        self.stdout.write(self.style.SUCCESS(
            f"\n{sent} department digest(s) sent, {len(results) - sent} quiet."
        ))

"""The 07:30 job. Builds the day's run before anyone reaches the office.

    python manage.py supply_chain_daily_run --company JIVO_OIL

Deliberately does NOT publish. The playbook puts a person between the computer
and the buyer's phone, because a sheet nobody looked at is how twenty-five wrong
alarms get sent to a department that then stops reading them.

``--publish`` exists for a company that has decided to skip the review, and says
so out loud in the output.
"""
from django.core.management.base import BaseCommand

from supply_chain.services import operations as ops
from supply_chain.services.daily_run import build_daily_run
from supply_chain.services.errors import SupplyChainError


class Command(BaseCommand):
    help = "Build today's supply-chain daily run."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--date", default=None, help="YYYY-MM-DD. Defaults to today.")
        parser.add_argument("--publish", action="store_true",
                            help="Review and publish without a human check.")

    def handle(self, *args, **options):
        run = build_daily_run(options["company"], run_date=options["date"])
        self.stdout.write(self.style.SUCCESS(
            f"{run.company_code} {run.run_date}: {run.red_count} red, {run.amber_count} amber, "
            f"{run.green_count} green, {run.unknown_count} unjudged, "
            f"{run.issue_count} data-quality issue(s)."
        ))
        if not run.is_credible:
            self.stdout.write(self.style.WARNING(
                f"  BLOCKED: {run.red_count} reds is beyond this company's credible limit. "
                "That points at the stock or PO data, not the factory. Fix the inputs first."
            ))
            return
        if not options["publish"]:
            self.stdout.write("  Waiting for the analyst to review before it is sent.")
            return

        try:
            ops.review_run(run, comment="Auto-reviewed by the scheduled job.")
            _run, message = ops.publish_run(run, comment=run.comment)
        except SupplyChainError as exc:
            self.stderr.write(f"  Not published: {exc.message}")
            return
        self.stdout.write(self.style.SUCCESS(
            f"  Published to {message['recipients']} user(s) with no human review."
        ))

"""Send the supply-chain procurement alarms. Intended for a daily cron.

    python manage.py send_supply_chain_alarms --company JIVO_OIL
    python manage.py send_supply_chain_alarms --company JIVO_OIL --dry-run

``--dry-run`` builds every digest and sends nothing, so the schedule can be
verified against production data without paging anyone.
"""
from django.core.management.base import BaseCommand

from supply_chain.services.alarms import send_supply_chain_alarms


class Command(BaseCommand):
    help = "Send supply-chain procurement alarms to their subscribed departments."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--forecast-id", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true",
                            help="Re-send even when the digest has not changed.")

    def handle(self, *args, **options):
        company_code = options["company"]
        company = None
        try:
            from company.models import Company

            company = Company.objects.filter(code=company_code).first()
        except Exception:  # noqa: BLE001 — company scoping is a nicety, not a gate
            company = None

        results = send_supply_chain_alarms(
            company_code, company=company, forecast_id=options["forecast_id"],
            force=options["force"], dry_run=options["dry_run"],
        )
        if not results:
            self.stdout.write(self.style.WARNING(
                f"No active alarm subscriptions for {company_code} — nobody would be told."
            ))
            return
        for r in results:
            if r["sent"]:
                self.stdout.write(self.style.SUCCESS(
                    f"  {r['subscription']}: sent to {r['recipients']} user(s) "
                    f"({r['matched']} material(s)) — {r['title']}"
                ))
            else:
                self.stdout.write(f"  {r['subscription']}: not sent — {r['reason']}")
                if options["dry_run"] and r.get("body"):
                    for line in r["body"].splitlines():
                        self.stdout.write(f"      {line}")

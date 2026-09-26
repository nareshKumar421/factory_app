"""Read everything now and make the plan, as the 7 pm job does.

    .venv/bin/python manage.py build_tomorrow_run --company JIVO_OIL [--for-date 2026-09-28] [--check]
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from tomorrow_run import services
from tomorrow_run.inputs import InputsUnavailable


class Command(BaseCommand):
    help = "Read the apps and make Tomorrow's run (the 7 pm read, by hand)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_OIL")
        parser.add_argument("--for-date", type=date.fromisoformat, default=None)
        parser.add_argument("--check", action="store_true", help="Also run today's 7 pm check first.")

    def handle(self, *args, **opts):
        company = Company.objects.filter(code=opts["company"]).first()
        if company is None:
            raise CommandError(f"No company {opts['company']}")
        if opts["check"]:
            check = services.run_check(company)
            self.stdout.write(f"check: {check.red if check else 'no plan for today'} red")
        try:
            plan = services.build(company, for_date=opts["for_date"], trigger="manual")
        except InputsUnavailable as e:
            raise CommandError(str(e))
        p = plan.plan
        self.stdout.write(self.style.SUCCESS(
            f"{company.code} {p['for_date']}: {p['total_l'] / 1000:.1f} T on {sum(1 for m in p['machines'].values() if m['jobs'])} "
            f"machines, {p['final_list']['skus']} items on the final list, {len(p['pending'])} waiting"
        ))
        for w in p["warnings"]:
            self.stdout.write(self.style.WARNING(w))

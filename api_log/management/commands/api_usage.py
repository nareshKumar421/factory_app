"""What the API is used for most, from the call log.

    manage.py api_usage                    # modules, last 7 days
    manage.py api_usage --by route --days 30
    manage.py api_usage --by user --company JIVO_OIL --writes

``--writes`` counts only POST, PUT, PATCH and DELETE: the work people did, with
the boards' and banners' polling left out.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Avg, Count, Max, Q
from django.utils import timezone

from api_log.middleware import WRITE_METHODS
from api_log.models import ApiCall

#: What each --by groups the calls on.
GROUPS = {
    "app": ["app_label"],
    "route": ["method", "route"],
    "user": ["user__email"],
    "company": ["company_code"],
}


class Command(BaseCommand):
    help = "The most used modules, endpoints, users or companies, from the API call log."

    def add_arguments(self, parser):
        parser.add_argument("--by", choices=GROUPS, default="app")
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--limit", type=int, default=30)
        parser.add_argument("--company", help="Only calls made under this Company-Code.")
        parser.add_argument("--writes", action="store_true", help="Only POST, PUT, PATCH and DELETE.")

    def handle(self, *args, by, days, limit, company, writes, **options):
        calls = ApiCall.objects.filter(started_at__gte=timezone.now() - timedelta(days=days))
        if company:
            calls = calls.filter(company_code=company)
        if writes:
            calls = calls.filter(method__in=WRITE_METHODS)

        fields = GROUPS[by]
        rows = (
            calls.values(*fields)
            .annotate(
                calls=Count("id"),
                users=Count("user", distinct=True),
                failed=Count("id", filter=Q(status_code__gte=400)),
                avg_ms=Avg("duration_ms"),
                max_ms=Max("duration_ms"),
            )
            .order_by("-calls")[:limit]
        )

        total = calls.aggregate(calls=Count("id"), users=Count("user", distinct=True))
        self.stdout.write(
            f"{total['calls']} calls by {total['users']} users in the last {days} days"
        )
        self.stdout.write(f"{'calls':>8} {'users':>6} {'failed':>7} {'avg ms':>7} {'max ms':>7}  {by}")
        for row in rows:
            name = " ".join(str(row[field] or "-") for field in fields)
            self.stdout.write(
                f"{row['calls']:>8} {row['users']:>6} {row['failed']:>7} "
                f"{round(row['avg_ms']):>7} {row['max_ms']:>7}  {name}"
            )

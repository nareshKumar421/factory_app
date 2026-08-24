"""Report scan failures at the dock and on BSTs.

Answers the question the rejection logging was added to answer: *which* scan
failures actually cost time at the dock, and were they knowable earlier?

    python manage.py report_scan_failures --days 30
    python manage.py report_scan_failures --since 2026-09-01 --context SALES_DISPATCH

``NOT_FOUND`` and ``REJECTED`` are reported separately on purpose. They are
different problems: a NOT_FOUND is usually cleared by a re-scan within a few
minutes, while a REJECTED needs somebody to make a decision — which is what
holds a truck. Averaging them together hides that.
"""
from collections import Counter
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max, Min
from django.utils import timezone

from barcode.models import ScanLog, ScanResult

FAILURE_RESULTS = (ScanResult.NOT_FOUND, ScanResult.REJECTED)


class Command(BaseCommand):
    help = "Summarise scan failures (NOT_FOUND + REJECTED) for docking and BST scans."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=30,
            help="Window ending now, in days (default 30). Ignored when --since is given.",
        )
        parser.add_argument(
            "--since", type=str, default=None,
            help="Start date, YYYY-MM-DD. Overrides --days.",
        )
        parser.add_argument(
            "--context", type=str, default=None,
            help="Limit to one context_ref_type, e.g. SALES_DISPATCH or BST_TRANSFER.",
        )
        parser.add_argument(
            "--top", type=int, default=15,
            help="How many reject codes / worst sessions to list (default 15).",
        )

    def handle(self, *args, **options):
        since = self._resolve_since(options)
        qs = ScanLog.objects.filter(scanned_at__gte=since)
        if options["context"]:
            qs = qs.filter(context_ref_type=options["context"])

        total = qs.count()
        if not total:
            self.stdout.write(self.style.WARNING("No scans in this window."))
            return

        self._write_header(since, options["context"], total)
        self._write_result_mix(qs, total)
        self._write_reject_codes(qs, options["top"])
        self._write_context_rates(qs)
        self._write_retries(qs, options["top"])

    # -- sections ---------------------------------------------------------

    def _write_header(self, since, context, total):
        scope = context or "all contexts"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nScan failures since {since:%Y-%m-%d %H:%M} — {scope} — {total:,} scans"
        ))

    def _write_result_mix(self, qs, total):
        self.stdout.write(self.style.MIGRATE_HEADING("\nBy result"))
        rows = qs.values("scan_result").annotate(n=Count("id")).order_by("-n")
        for row in rows:
            pct = 100.0 * row["n"] / total
            line = f"  {row['scan_result']:<12} {row['n']:>8,}  {pct:5.1f}%"
            self.stdout.write(
                self.style.ERROR(line) if row["scan_result"] in FAILURE_RESULTS
                else line
            )
        failures = sum(r["n"] for r in rows if r["scan_result"] in FAILURE_RESULTS)
        self.stdout.write(
            f"  {'FAILURE RATE':<12} {failures:>8,}  {100.0 * failures / total:5.1f}%"
        )

    def _write_reject_codes(self, qs, top):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nWhy scans were refused (REJECTED only — a rule said no)"
        ))
        rows = (
            qs.filter(scan_result=ScanResult.REJECTED)
            .values("reject_code")
            .annotate(n=Count("id"), boxes=Count("barcode_raw", distinct=True))
            .order_by("-n")[:top]
        )
        if not rows:
            self.stdout.write("  (none — no business rejections recorded in this window)")
            return
        self.stdout.write(f"  {'code':<28}{'scans':>8}{'distinct barcodes':>20}")
        for row in rows:
            code = row["reject_code"] or "(blank)"
            self.stdout.write(f"  {code:<28}{row['n']:>8,}{row['boxes']:>20,}")

    def _write_context_rates(self, qs):
        self.stdout.write(self.style.MIGRATE_HEADING("\nBy context"))
        counts = Counter()
        failures = Counter()
        successes = Counter()
        for row in qs.values("context_ref_type", "scan_result").annotate(n=Count("id")):
            key = row["context_ref_type"] or "(none)"
            counts[key] += row["n"]
            if row["scan_result"] in FAILURE_RESULTS:
                failures[key] += row["n"]
            elif row["scan_result"] == ScanResult.SUCCESS:
                successes[key] += row["n"]
        self.stdout.write(f"  {'context':<20}{'scans':>10}{'failed':>10}{'rate':>10}")
        for key, n in counts.most_common():
            if not successes[key]:
                # BST logs only failures (successes live in BSTBoxScan), so a rate
                # here would always read 100% and mean nothing.
                rate_text = "n/a"
            else:
                rate_text = f"{100.0 * failures[key] / n:.1f}%"
            self.stdout.write(f"  {key:<20}{n:>10,}{failures[key]:>10,}{rate_text:>10}")
        if any(not successes[k] for k in counts):
            self.stdout.write(
                "  n/a = only failed scans are logged for this context, so no rate "
                "can be computed."
            )

    def _write_retries(self, qs, top):
        """Barcodes scanned again and again — the operator standing at the dock."""
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nRetry storms (same barcode failing repeatedly)"
        ))
        rows = (
            qs.filter(scan_result__in=FAILURE_RESULTS)
            .values("barcode_raw", "context_ref_type", "context_ref_id")
            .annotate(attempts=Count("id"), first=Min("scanned_at"), last=Max("scanned_at"))
            .filter(attempts__gt=1)
            .order_by("-attempts")[:top]
        )
        if not rows:
            self.stdout.write("  (none — no barcode failed more than once)")
            return
        self.stdout.write(
            f"  {'barcode':<28}{'tries':>7}{'span':>10}  context"
        )
        for row in rows:
            span = row["last"] - row["first"]
            minutes = span.total_seconds() / 60
            span_text = f"{minutes:.0f}m" if minutes < 120 else f"{minutes / 60:.1f}h"
            context = f"{row['context_ref_type'] or '-'}#{row['context_ref_id'] or '-'}"
            self.stdout.write(
                f"  {row['barcode_raw'][:27]:<28}{row['attempts']:>7}{span_text:>10}  {context}"
            )

    # -- helpers ----------------------------------------------------------

    def _resolve_since(self, options):
        if options["since"]:
            try:
                parsed = datetime.strptime(options["since"], "%Y-%m-%d")
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM-DD") from exc
            return timezone.make_aware(parsed)
        return timezone.now() - timedelta(days=options["days"])

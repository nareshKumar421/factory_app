"""
Copy EXIM's monthly plan uploads (every version, with its rows).

Usage:
    python manage.py import_exim_monthly_plans                 # dry run: says what it would do
    python manage.py import_exim_monthly_plans --commit
    python manage.py import_exim_monthly_plans --commit --company JIVO_OIL

Reads EXIM's database through the read-only ``exim`` alias and never writes to
it. An upload already copied (by EXIM's id) is left alone, so a re-run only
adds the versions uploaded in EXIM since. A copied version keeps its month and
version number; if this company already has that version of that month (it was
uploaded here), the copy is skipped and reported.

It is a DRY RUN unless ``--commit`` is given.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, transaction

from exim.licence_import import LicenceImportProblem, resolve_company
from planning_purchase.models_monthly_plan import MonthlyPlanRow, MonthlyPlanUpload

ROW_FIELDS = (
    "code", "brand", "head", "category", "sub_category", "sku", "per_ltrs", "ltrs_per_box", "case_pack",
    "commodity_monthly", "commodity_w1", "commodity_w2", "commodity_w3", "commodity_w4",
    "premium_monthly", "premium_w1", "premium_w2", "premium_w3", "premium_w4",
    "ecom_planning", "total_planning", "source_row",
)


def read_exim(cursor) -> dict:
    cursor.execute(
        "SELECT id, month, version, title, source_file, uploaded_by, uploaded_at, notes FROM planning_uploads"
    )
    cols = [c[0] for c in cursor.description]
    uploads = [dict(zip(cols, r)) for r in cursor.fetchall()]
    cursor.execute("SELECT upload_id, " + ", ".join(ROW_FIELDS) + " FROM planning_rows")
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    return {"uploads": uploads, "rows": rows}


def import_plans(snapshot: dict, *, company) -> dict:
    counts = {"create": 0, "kept": 0, "clash": 0, "rows": 0}
    notes = []
    users = {u.email.lower(): u for u in get_user_model().objects.exclude(email="")}
    rows_by_upload = {}
    for row in snapshot["rows"]:
        rows_by_upload.setdefault(row["upload_id"], []).append(row)
    copied = set(MonthlyPlanUpload.objects.filter(company=company).exclude(exim_id=None)
                 .values_list("exim_id", flat=True))
    for exim in sorted(snapshot["uploads"], key=lambda u: (u["month"], u["version"])):
        if exim["id"] in copied:
            counts["kept"] += 1
            continue
        if MonthlyPlanUpload.objects.filter(company=company, month=exim["month"], version=exim["version"]).exists():
            counts["clash"] += 1
            notes.append(f"{exim['month']:%b %Y} v{exim['version']}: that version was uploaded here; left alone")
            continue
        label = (exim["uploaded_by"] or "").strip()
        upload = MonthlyPlanUpload.objects.create(
            company=company, month=exim["month"], version=exim["version"], title=exim["title"] or "",
            source_file=exim["source_file"] or "", uploaded_by=users.get(label.lower()),
            uploaded_by_label=label, notes=exim["notes"] or "", exim_id=exim["id"],
        )
        MonthlyPlanUpload.objects.filter(pk=upload.pk).update(uploaded_at=exim["uploaded_at"])
        lines = rows_by_upload.get(exim["id"], [])
        MonthlyPlanRow.objects.bulk_create(
            [MonthlyPlanRow(upload=upload, **{f: r[f] for f in ROW_FIELDS}) for r in lines], batch_size=500,
        )
        upload.recalculate_totals().save()
        counts["create"] += 1
        counts["rows"] += len(lines)
    return {"counts": counts, "notes": notes}


class Command(BaseCommand):
    help = "Copy EXIM's monthly plan uploads across. A dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Write the changes. Without it nothing is saved.")
        parser.add_argument("--company", default="JIVO_OIL", help="The company the plans belong to (default JIVO_OIL).")
        parser.add_argument("--database", default="exim", help="The alias EXIM is read through (default: exim).")

    def handle(self, *args, **options):
        alias = options["database"]
        if alias not in settings.DATABASES:
            raise CommandError(f"There is no {alias!r} database configured (EXIM_DB_* in config/settings.py).")
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing will be written\n"))
        try:
            company = resolve_company(options["company"])
        except LicenceImportProblem as exc:
            raise CommandError(str(exc)) from exc
        try:
            with connections[alias].cursor() as cursor:
                snapshot = read_exim(cursor)
        except DatabaseError as exc:
            raise CommandError(f"Could not read EXIM's monthly plans through {alias!r}: {exc}") from exc
        with transaction.atomic():
            report = import_plans(snapshot, company=company)
            if not options["commit"]:
                transaction.set_rollback(True)
        c = report["counts"]
        self.stdout.write(f"Into {company.code}:\n  monthly plans      {c['create']} create ({c['rows']} rows), "
                          f"{c['kept']} kept, {c['clash']} clash")
        for note in report["notes"]:
            self.stdout.write(self.style.WARNING(f"  {note}"))
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was written. Re-run with --commit."))

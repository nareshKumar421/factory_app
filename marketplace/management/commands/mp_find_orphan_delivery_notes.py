"""READ-ONLY: find delivery notes JI cut in SAP but has no record of.

Every note JI posts carries ``NumAtCard = MKT-<date>-<lead dispatch pk>``. A note
holding one of those refs was cut by the software, so JI should be able to name the
orders behind it. When it cannot, a post reached SAP and the response never came
back — the goods left, and nothing in JI says so.

That is DN 1507264771 (ref MKT-20260731-970, posted 4 Aug 2026): invisible to JI,
and re-cut minutes later as 1508264503, so the stock moved twice.

Writes nothing. Usage:

    python manage.py mp_find_orphan_delivery_notes
    python manage.py mp_find_orphan_delivery_notes --company JIVO_MART --days 90
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from marketplace.models import MarketplaceDispatch


class Command(BaseCommand):
    help = "READ-ONLY: list software-cut delivery notes that JI has no record of."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--days", type=int, default=120,
                            help="How far back to look (default 120).")

    def handle(self, *args, **opts):
        try:
            company = Company.objects.get(code=opts["company"])
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {opts['company']!r}.")

        rows = self._sap_notes(company, opts["days"])
        if rows is None:
            raise CommandError("Could not read the delivery notes from SAP.")

        known_nums = set(MarketplaceDispatch.objects
                         .exclude(sap_delivery_note_num="")
                         .values_list("sap_delivery_note_num", flat=True))
        known_refs = set(MarketplaceDispatch.objects
                         .exclude(sap_dn_ref="")
                         .values_list("sap_dn_ref", flat=True))

        orphans = [r for r in rows
                   if str(r["doc_num"]) not in known_nums and r["ref"] not in known_refs]

        self.stdout.write(
            f"{len(rows)} software-cut note(s) in the last {opts['days']} days · "
            f"{len(orphans)} with no record in JI\n")
        if not orphans:
            self.stdout.write(self.style.SUCCESS("Nothing orphaned."))
            return

        for r in orphans:
            lead = r["ref"].rsplit("-", 1)[-1]
            dispatch = MarketplaceDispatch.objects.filter(pk=lead).first() if lead.isdigit() else None
            self.stdout.write(self.style.WARNING(
                f"\n  DN {r['doc_num']} (DocEntry {r['doc_entry']}) {r['doc_date']}  "
                f"total {r['total']}"))
            self.stdout.write(f"      ref      {r['ref']}")
            self.stdout.write(f"      comments {r['comments'][:90]}")
            if dispatch:
                self.stdout.write(
                    f"      lead dispatch {dispatch.pk}: order {dispatch.order.order_id}, "
                    f"now on DN {dispatch.sap_delivery_note_num or '—'} "
                    f"({dispatch.sap_post_status})")
                if dispatch.sap_delivery_note_num:
                    self.stdout.write(self.style.ERROR(
                        "      → the goods on this note were cut AGAIN under that DN; "
                        "this one is a duplicate and should be cancelled in SAP."))
            else:
                self.stdout.write("      lead dispatch not found — investigate by hand.")

    def _sap_notes(self, company, days):
        """Delivery notes carrying a JI ref, newest first. None if SAP is unreachable."""
        try:
            from hdbcli import dbapi
            from sap_client.context import CompanyContext
            h = CompanyContext(company.code).hana
        except Exception as e:
            self.stderr.write(f"HANA unavailable: {e}")
            return None
        conn = None
        try:
            conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                                 password=h["password"], connectTimeout=20000)
            cur = conn.cursor()
            cur.execute(
                f'SELECT "DocNum","DocEntry","DocDate","NumAtCard","Comments","DocTotal" '
                f'FROM "{h["schema"]}"."ODLN" '
                f'WHERE "NumAtCard" LIKE \'MKT-%\' AND "CANCELED" = \'N\' '
                f'  AND "DocDate" >= ADD_DAYS(CURRENT_DATE, ?) '
                f'ORDER BY "DocEntry" DESC',
                [-abs(days)])
            rows = [{
                "doc_num": n, "doc_entry": e, "doc_date": str(d)[:10],
                "ref": ref or "", "comments": com or "", "total": t,
            } for n, e, d, ref, com, t in cur.fetchall()]
            cur.close()
            return rows
        except Exception as e:
            self.stderr.write(f"Delivery-note query failed: {e}")
            return None
        finally:
            if conn is not None:
                conn.close()

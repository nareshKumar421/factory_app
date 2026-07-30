"""Fill item-name fields in the masters with the authoritative SAP name.

Component/mapping item NAMES are copied when a combo or mapping is created and drift
over time — stale labels (e.g. an item shown as "10ML" when it's really "200 MLS"),
blanks, or inconsistent variants of the same code. This reads the true name for every
referenced item code from SAP (OITM) and updates:

  * ComboComponent.item_name
  * SkuMapping.fg_item_name   (RAW mappings)
  * SkuMappingOption.fg_item_name

so every label matches the real SAP item. Item CODES are never changed — only names.
Idempotent. Dry-run by default; pass ``--apply`` to write.

    python manage.py mp_sync_item_names
    python manage.py mp_sync_item_names --apply
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from marketplace.models import ComboComponent, SkuMapping, SkuMappingOption, SkuType


class Command(BaseCommand):
    help = "Sync masters item-name fields to the authoritative SAP (OITM) name."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument("--apply", action="store_true", help="Write changes (otherwise dry run).")

    def _sap_names(self, company_code, codes):
        from hdbcli import dbapi
        from sap_client.context import CompanyContext
        h = CompanyContext(company_code).hana
        sch = h["schema"]
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        try:
            cur = conn.cursor()
            ph = ",".join(["?"] * len(codes))
            cur.execute(f'SELECT "ItemCode","ItemName" FROM "{sch}"."OITM" WHERE "ItemCode" IN ({ph})', list(codes))
            names = {r[0]: (r[1] or "") for r in cur.fetchall()}
            cur.close()
        finally:
            conn.close()
        return names

    def handle(self, *args, **opts):
        try:
            company = Company.objects.get(code=opts["company"])
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {opts['company']!r}")
        channel, apply = opts["channel"], opts["apply"]

        comps = list(ComboComponent.objects.filter(combo__company=company, combo__channel=channel))
        raws = list(SkuMapping.objects.filter(company=company, channel=channel, sku_type=SkuType.RAW).exclude(fg_item_code=""))
        opts_qs = list(SkuMappingOption.objects.filter(mapping__company=company, mapping__channel=channel).exclude(fg_item_code=""))

        codes = {c.item_code.strip() for c in comps if c.item_code.strip()}
        codes |= {m.fg_item_code.strip() for m in raws}
        codes |= {o.fg_item_code.strip() for o in opts_qs}
        codes = {c for c in codes if c}
        if not codes:
            self.stdout.write("No item codes to sync.")
            return

        names = self._sap_names(company.code, codes)
        self.stdout.write(f"{len(codes)} distinct item codes · {sum(1 for c in codes if c not in names)} not found in SAP.")

        changed = 0
        samples = []

        def fix(obj, field, code):
            nonlocal changed
            true = names.get(code)
            if not true:
                return  # unknown in SAP — leave as-is
            cur = getattr(obj, field) or ""
            if cur != true:
                if len(samples) < 25:
                    samples.append(f"  {code}: {cur!r} -> {true!r}")
                setattr(obj, field, true)
                if apply:
                    obj.save(update_fields=[field])
                changed += 1

        for c in comps:
            fix(c, "item_name", c.item_code.strip())
        for m in raws:
            fix(m, "fg_item_name", m.fg_item_code.strip())
        for o in opts_qs:
            fix(o, "fg_item_name", o.fg_item_code.strip())

        self.stdout.write("\nName corrections:")
        for s in samples:
            self.stdout.write(s)
        if changed > len(samples):
            self.stdout.write(f"  … +{changed - len(samples)} more")

        verb = "WROTE" if apply else "DRY RUN — would fix"
        self.stdout.write(self.style.SUCCESS(f"\n{verb}: {changed} name field(s) corrected."))
        if not apply:
            self.stdout.write("Re-run with --apply to commit.")

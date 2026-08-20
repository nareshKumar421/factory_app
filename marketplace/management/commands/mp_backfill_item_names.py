"""Fill in SAP item names the masters were saved without, and flag unknown codes.

A mapping saved while the SAP item master is unreachable keeps whatever name was
typed — usually nothing — because ``item_master.apply_sap_name`` can only copy a
name it was able to read. That is deliberate: masters must stay editable during an
outage. The cost is that the row is left both nameless and unverified, and nothing
records which rows those are.

It shows up as a Consolidated stock list full of item codes with "—" for a name,
which is what happened to the masters rebuilt on 19 Aug 2026 while HANA was
refusing the connection.

This repairs them: every FG code the masters hold is looked up in OITM, blank names
are filled from SAP, names that disagree with SAP are corrected, and any code SAP
has never heard of is reported — that one cannot be fixed here, because a code the
item master does not know ships nothing.

Dry run by default::

    python manage.py mp_backfill_item_names
    python manage.py mp_backfill_item_names --apply
    python manage.py mp_backfill_item_names --company JIVO_MART --apply
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company
from marketplace.models import (
    ComboComponent,
    ComboComponentOption,
    SkuMapping,
    SkuMappingOption,
    SkuType,
)

# (model, code field, name field, label) — every place a SAP item code is stored.
TARGETS = (
    (ComboComponent, "item_code", "item_name", "combo components"),
    (ComboComponentOption, "item_code", "item_name", "component alternatives"),
    (SkuMapping, "fg_item_code", "fg_item_name", "RAW mappings"),
    (SkuMappingOption, "fg_item_code", "fg_item_name", "mapping options"),
)


class Command(BaseCommand):
    help = "Fill missing SAP item names on marketplace masters; flag unknown codes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--apply", action="store_true",
                            help="Write the names (otherwise report only).")

    def handle(self, *args, **opts):
        try:
            company = Company.objects.get(code=opts["company"])
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {opts['company']!r}.")

        rows = self._rows()
        codes = sorted({c for _obj, c, _f in rows})
        if not codes:
            self.stdout.write(self.style.SUCCESS("No item codes in the masters."))
            return

        from marketplace.services.item_master import lookup_names

        known = lookup_names(company.code, codes)
        if known is None:
            raise CommandError(
                "The SAP item master could not be read, so there are no names to copy. "
                "This is the same outage that left the rows blank — fix the HANA "
                "connection first (check HANA_USER / HANA_PASSWORD), then re-run.")

        missing = [c for c in codes if c not in known]
        blank = [(o, c, f) for o, c, f in rows if not (getattr(o, f) or "").strip()]
        wrong = [(o, c, f) for o, c, f in rows
                 if c in known and (getattr(o, f) or "").strip()
                 and getattr(o, f).strip() != known[c]]

        self.stdout.write(
            f"{len(rows)} stored code(s) · {len(codes)} distinct · "
            f"{len(known)} found in SAP")
        self.stdout.write(f"    blank names to fill        {len(blank):>5}")
        self.stdout.write(f"    names disagreeing with SAP {len(wrong):>5}")
        if missing:
            self.stdout.write(self.style.ERROR(
                f"\n{len(missing)} code(s) SAP has never heard of — these ship nothing "
                f"and must be corrected by hand:"))
            for c in missing:
                where = ", ".join(sorted({lbl for o, cc, f, lbl in self._rows(labelled=True)
                                          if cc == c}))
                self.stdout.write(self.style.ERROR(f"    {c}   ({where})"))

        if not opts["apply"]:
            self.stdout.write(self.style.SUCCESS("\nDry run — nothing written."))
            return

        filled = corrected = 0
        with transaction.atomic():
            for obj, code, field in blank:
                if code in known:
                    setattr(obj, field, known[code])
                    obj.save(update_fields=[field])
                    filled += 1
            for obj, code, field in wrong:
                setattr(obj, field, known[code])
                obj.save(update_fields=[field])
                corrected += 1
        self.stdout.write(self.style.SUCCESS(
            f"\nFilled {filled} blank name(s), corrected {corrected}."))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"{len(missing)} unknown code(s) left untouched — see above."))

    def _rows(self, labelled=False):
        """Every (object, code, name field) the masters hold a SAP item code in."""
        out = []
        for model, code_field, name_field, label in TARGETS:
            qs = model.objects.all()
            if model is SkuMapping:
                qs = qs.filter(sku_type=SkuType.RAW)
            for obj in qs:
                code = (getattr(obj, code_field) or "").strip()
                if not code:
                    continue
                out.append((obj, code, name_field, label) if labelled
                           else (obj, code, name_field))
        return out

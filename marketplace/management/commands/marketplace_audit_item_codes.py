"""Audit marketplace master data against the SAP item master.

Reports codes SAP has never heard of and names that disagree with SAP's, across
combo components, their alternatives, and SKU mappings (with their alternatives).

Reports only, unless ``--fix`` is given. Nothing here runs on deploy: a bad code
is a business decision to correct, not something to silently rewrite under an
operator who may be mid-investigation. ``--fix`` repairs names only — it never
invents a code, because it cannot know which product was meant.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from company.models import Company
from marketplace.models import (
    ComboComponent, ComboComponentOption, SkuMapping, SkuMappingOption,
)
from marketplace.services.item_master import lookup_names


class Command(BaseCommand):
    help = "Report marketplace item codes that SAP does not know, or whose names disagree."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", help="Company code to audit (default: every active company).")
        parser.add_argument(
            "--fix", action="store_true",
            help="Rewrite mismatched names to SAP's. Never touches item codes.")

    def handle(self, *args, **options):
        companies = Company.objects.filter(is_active=True)
        if options.get("company"):
            companies = companies.filter(code=options["company"])
        if not companies:
            self.stderr.write("No matching company.")
            return

        for company in companies:
            self._audit(company, fix=options["fix"])

    # Each row: (label, object, code attribute, name attribute).
    # Only ComboDefinition and SkuMapping carry is_active/updated_at; their child
    # rows are plain models, so activeness is filtered on the parent alone.
    def _rows(self, company):
        for c in ComboComponent.objects.filter(
                combo__company=company, combo__is_active=True
        ).select_related("combo"):
            yield (f"combo {c.combo.code} component", c, "item_code", "item_name")
        for o in ComboComponentOption.objects.filter(
                component__combo__company=company, component__combo__is_active=True,
        ).select_related("component__combo"):
            yield (f"combo {o.component.combo.code} option", o, "item_code", "item_name")
        for m in SkuMapping.objects.filter(company=company, is_active=True):
            if (m.fg_item_code or "").strip():
                yield (f"mapping {m.fsn or m.marketplace_sku}", m, "fg_item_code", "fg_item_name")
        for o in SkuMappingOption.objects.filter(
                mapping__company=company, mapping__is_active=True,
        ).select_related("mapping"):
            if (o.fg_item_code or "").strip():
                yield (f"mapping {o.mapping.fsn or o.mapping.marketplace_sku} option",
                       o, "fg_item_code", "fg_item_name")

    def _audit(self, company, *, fix):
        rows = list(self._rows(company))
        if not rows:
            self.stdout.write(f"{company.code}: nothing to audit.")
            return

        codes = {getattr(obj, code_attr) for _, obj, code_attr, _ in rows}
        known = lookup_names(company.code, codes)
        if not known:
            # Same rule as the serializers: an unreachable master says nothing.
            self.stderr.write(
                f"{company.code}: SAP item master unreachable — cannot audit.")
            return

        missing, mismatched = [], []
        for label, obj, code_attr, name_attr in rows:
            code = (getattr(obj, code_attr) or "").strip()
            sap_name = known.get(code)
            if sap_name is None:
                missing.append((label, obj, code))
                continue
            stored = (getattr(obj, name_attr) or "").strip()
            if stored != sap_name:
                mismatched.append((label, obj, code, stored, sap_name, name_attr))

        self.stdout.write(
            f"\n{company.code}: {len(rows)} item reference(s) — "
            f"{len(missing)} unknown, {len(mismatched)} name mismatch(es)")

        for label, _obj, code in missing:
            self.stdout.write(
                f"  MISSING   {code:<14s} {label}  (not in the SAP item master)")

        for label, obj, code, stored, sap_name, name_attr in mismatched:
            self.stdout.write(f"  MISMATCH  {code:<14s} {label}")
            self.stdout.write(f"              stored: {stored!r}")
            self.stdout.write(f"              SAP   : {sap_name!r}")

        if not fix:
            if mismatched:
                self.stdout.write(
                    "\n  Re-run with --fix to rewrite the names to SAP's. "
                    "Codes are never changed automatically.")
            if missing:
                self.stdout.write(
                    "  Unknown codes need a person: only you know which product "
                    "was meant.")
            return

        with transaction.atomic():
            for _label, obj, _code, _stored, sap_name, name_attr in mismatched:
                setattr(obj, name_attr, sap_name)
                fields = [name_attr]
                # Child rows are plain models with no updated_at to bump.
                if hasattr(obj, "updated_at"):
                    fields.append("updated_at")
                obj.save(update_fields=fields)
        self.stdout.write(f"\n  Repaired {len(mismatched)} name(s). "
                          f"{len(missing)} unknown code(s) left for you.")

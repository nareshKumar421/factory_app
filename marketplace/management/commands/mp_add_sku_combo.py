"""Map a marketplace SKU/FSN that ships a BUNDLE onto a combo of stocked items.

Some marketplace SKUs are bundles (e.g. "Pomace 5L + Extra Virgin 200ML"). They
can't be a RAW mapping (which points at ONE item) — they need a ComboDefinition of
their component finished goods so orders resolve, scan, and post a delivery note.

This command creates/refreshes that combo from ``--components`` (looking up each
item's name + UOM from existing combo components when available) and points the
SKU/FSN mapping at it. Idempotent. Dry-run by default; pass ``--apply`` to write.

    python manage.py mp_add_sku_combo --sku "Jivo-Pomace-5L-EV-200ML_New" \
        --fsn EDOHZPG2NKXDZZPJ --name "Jivo-Pomace-5L+Extra-Virgin-200ML" \
        --components FG0000008:1,FG0000381:1 --apply
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company
from marketplace.models import (
    ComboComponent, ComboComponentType, ComboDefinition, SkuMapping, SkuType,
)


class Command(BaseCommand):
    help = "Map a SKU/FSN bundle onto a combo of finished-good components."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument("--sku", required=True, help="marketplace SKU")
        parser.add_argument("--fsn", default="", help="marketplace FSN (optional)")
        parser.add_argument("--name", default="", help="combo name (defaults to the SKU)")
        parser.add_argument("--code", default="", help="combo code (defaults to a slug of the SKU)")
        parser.add_argument("--components", required=True,
                            help="comma list of ITEMCODE:QTY, e.g. FG0000008:1,FG0000381:1")
        parser.add_argument("--apply", action="store_true", help="Write changes (otherwise dry run).")

    def handle(self, *args, **opts):
        try:
            company = Company.objects.get(code=opts["company"])
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {opts['company']!r}")
        channel = opts["channel"]
        sku = opts["sku"].strip()
        fsn = opts["fsn"].strip()
        name = (opts["name"].strip() or sku)[:200]
        code = (opts["code"].strip() or ("MP-" + sku.upper().replace(" ", "-")))[:50]

        # Parse "CODE:QTY,CODE:QTY".
        comps = []
        for part in opts["components"].split(","):
            part = part.strip()
            if not part:
                continue
            ic, _, q = part.partition(":")
            ic = ic.strip()
            if not ic:
                continue
            comps.append((ic, Decimal(q.strip() or "1")))
        if not comps:
            raise CommandError("No valid components parsed from --components.")

        # Best-effort item name / uom from existing combo components — pick the MOST
        # COMMON name for the code (component data occasionally carries a stray label).
        def meta(item_code):
            from collections import Counter
            rows = ComboComponent.objects.filter(
                combo__company=company, combo__channel=channel, item_code=item_code,
            ).exclude(item_name="")
            names = Counter(r.item_name for r in rows)
            uoms = Counter(r.uom for r in rows if r.uom)
            name = names.most_common(1)[0][0] if names else ""
            uom = uoms.most_common(1)[0][0] if uoms else ""
            return name, uom

        self.stdout.write(f"Combo {code!r} '{name}' [{channel}] components:")
        for ic, q in comps:
            nm, uom = meta(ic)
            self.stdout.write(f"  {q} x {ic}  {nm or '(name unknown)'}")
        self.stdout.write(f"SKU mapping: {sku!r}  FSN {fsn or '—'}  ->  combo {code!r}")

        @transaction.atomic
        def run():
            combo, _made = ComboDefinition.objects.get_or_create(
                company=company, channel=channel, code=code, defaults={"name": name},
            )
            if combo.name != name and name:
                combo.name = name
                combo.save(update_fields=["name"])
            combo.components.all().delete()
            ComboComponent.objects.bulk_create([
                ComboComponent(
                    combo=combo, component_type=ComboComponentType.FG,
                    item_code=ic, item_name=meta(ic)[0], quantity=q, uom=meta(ic)[1],
                )
                for ic, q in comps
            ])
            mapping, _m = SkuMapping.objects.get_or_create(
                company=company, channel=channel, marketplace_sku=sku,
                defaults={"sku_type": SkuType.COMBO, "combo": combo, "fsn": fsn, "is_active": True},
            )
            mapping.sku_type = SkuType.COMBO
            mapping.combo = combo
            mapping.fg_item_code = ""
            if fsn:
                mapping.fsn = fsn
            mapping.is_active = True
            mapping.save()
            if not opts["apply"]:
                transaction.set_rollback(True)

        run()
        verb = "WROTE" if opts["apply"] else "DRY RUN — would write"
        self.stdout.write(self.style.SUCCESS(f"\n{verb}: combo {code!r} + SKU mapping for {sku!r}."))
        if not opts["apply"]:
            self.stdout.write("Re-run with --apply to commit.")

"""Sync SAP Sales BOMs into marketplace combos so delivery notes ship the
STOCKED component items, not the non-inventory sale-BOM parent.

Background: some SKU mappings point at SAP *Sales BOM* parents (OITM.TreeType='S',
InvntItem='N') — e.g. ``SL0000121`` "MUSTARD 5 LTR + 1 LTR". A Sales BOM parent
holds no stock; its children (``FG…``/``SC…``) do. Posting a delivery note for the
parent fails with ``-10 Quantity falls into negative inventory``. This command
reads each parent's components from SAP (``ITT1``), records them as a
:class:`ComboDefinition`, and repoints the mapping to that combo — after which
``resolve_service`` explodes the SKU into its stocked component lines.

Idempotent and repeatable. Dry-run by default; pass ``--apply`` to write.

    python manage.py mp_sync_sale_boms                    # dry run (JIVO_MART/FLIPKART)
    python manage.py mp_sync_sale_boms --apply            # write combos + repoint mappings
    python manage.py mp_sync_sale_boms --channel FLIPKART --company JIVO_MART --apply
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
    help = "Sync SAP Sales BOMs into combos and repoint SKU mappings to them."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument("--apply", action="store_true", help="Write changes (otherwise dry run).")

    def handle(self, *args, **opts):
        company_code, channel, apply = opts["company"], opts["channel"], opts["apply"]
        try:
            company = Company.objects.get(code=company_code)
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {company_code!r}")

        mappings = list(
            SkuMapping.objects.filter(company=company, channel=channel, is_active=True)
        )
        raw_targets = {
            (m.fg_item_code or "").strip()
            for m in mappings
            if m.sku_type == SkuType.RAW and (m.fg_item_code or "").strip()
        }
        # Also flatten combo components that themselves point at a sale-BOM (nested).
        combo_comp_codes = {
            (c.item_code or "").strip()
            for c in ComboComponent.objects.filter(
                combo__company=company, combo__channel=channel
            )
            if (c.item_code or "").strip()
        }
        candidates = sorted(raw_targets | combo_comp_codes)
        if not candidates:
            self.stdout.write("No mapping/combo item codes — nothing to do.")
            return

        parents, children = self._read_boms(company_code, candidates)
        sale_boms = {c for c in candidates if parents.get(c, {}).get("tree") == "S"}
        self.stdout.write(
            f"{len(mappings)} active mappings · {len(raw_targets)} RAW item codes · "
            f"{len(combo_comp_codes)} combo-component codes · "
            f"{len(sale_boms)} are SAP Sales BOMs (TreeType='S')."
        )
        if not sale_boms:
            self.stdout.write("No sale-BOM references to convert.")
            return

        missing = [c for c in sale_boms if not children.get(c)]
        if missing:
            self.stdout.write(self.style.WARNING(f"Sale BOMs with NO components (skipped): {missing}"))

        converted = created_defs = flattened = 0

        @transaction.atomic
        def run():
            nonlocal converted, created_defs, flattened
            for code in sorted(sale_boms):
                comps = children.get(code)
                if not comps:
                    continue
                combo, made = ComboDefinition.objects.get_or_create(
                    company=company, channel=channel, code=code,
                    defaults={"name": parents.get(code, {}).get("name", "")[:200]},
                )
                if made:
                    created_defs += 1
                elif not combo.name:
                    combo.name = parents.get(code, {}).get("name", "")[:200]
                    combo.save(update_fields=["name"])
                # Rebuild components from SAP (source of truth).
                combo.components.all().delete()
                ComboComponent.objects.bulk_create([
                    ComboComponent(
                        combo=combo, component_type=ComboComponentType.FG,
                        item_code=ch["item_code"], item_name=ch["item_name"][:200],
                        quantity=Decimal(str(ch["quantity"])), uom=ch["uom"][:20],
                    )
                    for ch in comps
                ])
                # Repoint every RAW mapping whose target is this sale BOM.
                for m in mappings:
                    if m.sku_type == SkuType.RAW and (m.fg_item_code or "").strip() == code:
                        m.sku_type = SkuType.COMBO
                        m.combo = combo
                        m.save(update_fields=["sku_type", "combo"])
                        converted += 1
                comp_str = " + ".join(f"{c['quantity']}x{c['item_code']}" for c in comps)
                self.stdout.write(f"  {code} '{parents.get(code, {}).get('name','')}' -> {comp_str}")

            # Pass 2: flatten combo components that point at a sale-BOM into its
            # stocked children (child qty × the component qty).
            nested = list(ComboComponent.objects.filter(
                combo__company=company, combo__channel=channel,
                item_code__in=sorted(sale_boms),
            ).select_related("combo"))
            for comp in nested:
                kids = children.get((comp.item_code or "").strip())
                if not kids:
                    continue
                parent_qty = Decimal(str(comp.quantity))
                for ch in kids:
                    add_qty = parent_qty * Decimal(str(ch["quantity"]))
                    existing = comp.combo.components.filter(
                        item_code=ch["item_code"], component_type=ComboComponentType.FG,
                    ).exclude(pk=comp.pk).first()
                    if existing:
                        existing.quantity = Decimal(str(existing.quantity)) + add_qty
                        existing.save(update_fields=["quantity"])
                    else:
                        ComboComponent.objects.create(
                            combo=comp.combo, component_type=ComboComponentType.FG,
                            item_code=ch["item_code"], item_name=ch["item_name"][:200],
                            quantity=add_qty, uom=ch["uom"][:20],
                        )
                self.stdout.write(
                    f"  flatten combo '{comp.combo.code}': {comp.quantity}x{comp.item_code} -> "
                    + " + ".join(f"{parent_qty*Decimal(str(k['quantity']))}x{k['item_code']}" for k in kids)
                )
                comp.delete()
                flattened += 1

            if not apply:
                transaction.set_rollback(True)

        run()
        verb = "WROTE" if apply else "DRY RUN — would write"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {created_defs} new combo definitions, {converted} mappings repointed to COMBO, "
            f"{flattened} nested sale-BOM combo components flattened."
        ))
        if not apply:
            self.stdout.write("Re-run with --apply to commit.")

    def _read_boms(self, company_code, item_codes):
        """Return (parents, children):
        parents  = {code: {"name", "tree", "invnt"}}
        children = {father: [{"item_code","item_name","quantity","uom"}, …]}
        Reads SAP HANA directly (OITM + ITT1)."""
        from hdbcli import dbapi
        from sap_client.context import CompanyContext

        h = CompanyContext(company_code).hana
        sch = h["schema"]
        codes = list(item_codes)
        ph = ",".join(["?"] * len(codes))
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        parents, children = {}, {}
        try:
            cur = conn.cursor()
            cur.execute(
                f'SELECT "ItemCode","ItemName","TreeType","InvntItem" FROM "{sch}"."OITM" '
                f'WHERE "ItemCode" IN ({ph})', codes)
            for ic, name, tree, invnt in cur.fetchall():
                parents[ic] = {"name": name or "", "tree": tree, "invnt": invnt}
            cur.execute(
                f'SELECT "Father","Code","Quantity","Warehouse" FROM "{sch}"."ITT1" '
                f'WHERE "Father" IN ({ph}) ORDER BY "Father","ChildNum"', codes)
            rows = cur.fetchall()
            child_codes = sorted({r[1] for r in rows})
            names = {}
            uoms = {}
            if child_codes:
                cph = ",".join(["?"] * len(child_codes))
                cur.execute(
                    f'SELECT "ItemCode","ItemName","InvntryUom" FROM "{sch}"."OITM" '
                    f'WHERE "ItemCode" IN ({cph})', child_codes)
                for ic, nm, uom in cur.fetchall():
                    names[ic] = nm or ""
                    uoms[ic] = uom or ""
            for father, code, qty, wh in rows:
                children.setdefault(father, []).append({
                    "item_code": code, "item_name": names.get(code, ""),
                    "quantity": qty, "uom": uoms.get(code, ""),
                })
            cur.close()
        finally:
            conn.close()
        return parents, children

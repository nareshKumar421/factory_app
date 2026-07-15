"""READ-ONLY: validate that every marketplace SKU mapping points to a REAL SAP item.

For each active ``SkuMapping`` it collects the SAP item code(s) it resolves to —
``fg_item_code`` for RAW mappings, or every combo component ``item_code`` for
COMBO mappings — and checks each one exists in SAP (Service Layer ``Items``).

Reports, per mapping: the sheet SKU, its type, the SAP code(s), and whether each
code was FOUND or MISSING in SAP. Writes nothing. Usage:

    python manage.py mp_validate_sku_mappings --company JIVO_MART --channel FLIPKART
"""
import requests
from django.core.management.base import BaseCommand

from marketplace.models import ComboComponentType, SkuMapping, SkuType
from sap_client.registry import get_company_config
from sap_client.service_layer.auth import ServiceLayerSession

requests.packages.urllib3.disable_warnings()


class Command(BaseCommand):
    help = "READ-ONLY: check each SKU mapping's SAP item code exists in SAP."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_MART")
        parser.add_argument("--channel", default="FLIPKART")

    def handle(self, *args, **o):
        from company.models import Company
        company = Company.objects.get(code=o["company"])
        mappings = list(
            SkuMapping.objects.filter(company=company, channel=o["channel"])
            .select_related("combo").prefetch_related("combo__components")
        )
        if not mappings:
            self.stdout.write(self.style.WARNING("No SKU mappings for this company/channel."))
            return

        # (sku, type, [sap_codes]) per mapping
        rows = []
        wanted = set()
        for m in mappings:
            if m.sku_type == SkuType.COMBO and m.combo_id:
                codes = [c.item_code for c in m.combo.components.all()]
            else:
                codes = [m.fg_item_code] if m.fg_item_code else []
            rows.append((m.marketplace_sku, m.sku_type, codes))
            wanted.update(c.strip() for c in codes if c and c.strip())

        # Look each code up in SAP (batched by OData $filter).
        found = self._sap_item_exists(o["company"], sorted(wanted))

        self.stdout.write(self.style.WARNING(
            f"\nSKU mappings for {o['company']}/{o['channel']}  "
            f"({len(mappings)} mappings, {len(wanted)} distinct SAP codes)\n"
        ))
        ok_maps, bad_maps = 0, 0
        for sku, stype, codes in rows:
            if not codes:
                self.stdout.write(f"  ✗ {sku:22} {stype:6} → (no SAP item code set)")
                bad_maps += 1
                continue
            marks = []
            all_ok = True
            for c in codes:
                hit = found.get(c.strip(), False)
                all_ok = all_ok and hit
                marks.append(f"{c}{'✓' if hit else '✗MISSING'}")
            self.stdout.write(f"  {'✓' if all_ok else '✗'} {sku:22} {stype:6} → " + "  ".join(marks))
            ok_maps += 1 if all_ok else 0
            bad_maps += 0 if all_ok else 1

        self.stdout.write(self.style.WARNING(
            f"\n{ok_maps} mapping(s) fully valid · {bad_maps} need fixing "
            f"(SAP code missing / not set)."
        ))
        missing = sorted(c for c in wanted if not found.get(c, False))
        if missing:
            self.stdout.write("Codes not found in SAP: " + ", ".join(missing))

    def _sap_item_exists(self, company_code, codes):
        """Return {item_code: bool} — whether each exists in SAP Items."""
        if not codes:
            return {}
        cfg = get_company_config(company_code)["service_layer"]
        base = cfg["base_url"]
        cookies = ServiceLayerSession(cfg).login()
        result = {c: False for c in codes}
        CHUNK = 20
        for i in range(0, len(codes), CHUNK):
            chunk = codes[i:i + CHUNK]
            flt = " or ".join(f"ItemCode eq '{c}'" for c in chunk)
            r = requests.get(
                f"{base}/b1s/v2/Items?$select=ItemCode&$filter={flt}&$top={CHUNK}",
                cookies=cookies, timeout=30, verify=False,
            )
            if r.status_code == 200:
                for it in r.json().get("value", []):
                    result[it.get("ItemCode")] = True
        return result

"""READ-ONLY: find a safe test customer + an item that has stock (for a test DN)."""
import requests
from django.core.management.base import BaseCommand

from sap_client.registry import get_company_config
from sap_client.service_layer.auth import ServiceLayerSession

requests.packages.urllib3.disable_warnings()


class Command(BaseCommand):
    help = "READ-ONLY: locate a test customer and in-stock items for a trial delivery note."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_MART")
        parser.add_argument("--items", default="FG0000329,FG0000151,FG0000226")

    def handle(self, *args, **opts):
        cfg = get_company_config(opts["company"])["service_layer"]
        base = cfg["base_url"]
        cookies = ServiceLayerSession(cfg).login()

        def get(path):
            r = requests.get(f"{base}/b1s/v2/{path}", cookies=cookies, timeout=30, verify=False)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, r.text

        # Customers whose name looks like a test/sample account
        for term in ("TEST", "SAMPLE", "JIVO", "CASH"):
            code, data = get(
                f"BusinessPartners?$select=CardCode,CardName&$filter=CardType eq 'cCustomer' and contains(CardName,'{term}')&$top=6"
            )
            rows = (data.get("value") if isinstance(data, dict) else []) or []
            if rows:
                self.stdout.write(self.style.WARNING(f"\n== Customers matching '{term}'  [{code}] =="))
                for bp in rows:
                    self.stdout.write(f"  {bp.get('CardCode'):12} {bp.get('CardName')}")

        # Stock per warehouse for candidate items
        self.stdout.write(self.style.WARNING("\n== Item stock by warehouse =="))
        for item in [s.strip() for s in opts["items"].split(",") if s.strip()]:
            code, data = get(
                f"Items('{item}')?$select=ItemCode,ItemName,InventoryUOM,ItemWarehouseInfoCollection"
            )
            if code != 200 or not isinstance(data, dict):
                self.stdout.write(f"  {item}: [{code}] {str(data)[:120]}")
                continue
            in_stock = [
                (w.get("WarehouseCode"), w.get("InStock"))
                for w in data.get("ItemWarehouseInfoCollection", [])
                if float(w.get("InStock") or 0) > 0
            ]
            self.stdout.write(f"  {item} ({data.get('ItemName')}) UoM={data.get('InventoryUOM')}")
            for wh, qty in in_stock[:8]:
                self.stdout.write(f"      {wh:10} InStock={qty}")
            if not in_stock:
                self.stdout.write("      (no positive stock in any warehouse)")

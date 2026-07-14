"""READ-ONLY SAP exploration for building the marketplace Delivery Note.

Logs into the Service Layer for a company and prints a small sample of the master
data a Delivery Note needs (customers, warehouses, items + UoM, tax, series).
Writes NOTHING. Usage:

    python manage.py mp_sap_explore --company JIVO_MART
"""
import json
import requests
from django.core.management.base import BaseCommand

from sap_client.registry import get_company_config
from sap_client.service_layer.auth import ServiceLayerSession

requests.packages.urllib3.disable_warnings()  # self-signed SAP cert


class Command(BaseCommand):
    help = "READ-ONLY: sample SAP master data for the marketplace delivery note."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_MART")

    def handle(self, *args, **opts):
        cfg = get_company_config(opts["company"])["service_layer"]
        base = cfg["base_url"]
        self.stdout.write(f"Base URL: {base}  CompanyDB: {cfg['company_db']}")
        cookies = ServiceLayerSession(cfg).login()
        self.stdout.write(self.style.SUCCESS("Login OK"))

        def get(path):
            r = requests.get(f"{base}/b1s/v2/{path}", cookies=cookies, timeout=30, verify=False)
            return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)

        # Customers (delivery-note CardCode)
        code, data = get("BusinessPartners?$select=CardCode,CardName,CardType&$filter=CardType eq 'cCustomer'&$top=8")
        self.stdout.write(self.style.WARNING(f"\n== Customers (CardType=C)  [{code}] =="))
        for bp in (data.get("value") if isinstance(data, dict) else []) or []:
            self.stdout.write(f"  {bp.get('CardCode'):12} {bp.get('CardName')}")

        # Warehouses
        code, data = get("Warehouses?$select=WarehouseCode,WarehouseName&$top=12")
        self.stdout.write(self.style.WARNING(f"\n== Warehouses  [{code}] =="))
        for w in (data.get("value") if isinstance(data, dict) else []) or []:
            self.stdout.write(f"  {w.get('WarehouseCode'):10} {w.get('WarehouseName')}")

        # Items + UoM (a few sellable items)
        code, data = get("Items?$select=ItemCode,ItemName,SalesUnit,InventoryUOM,SalesItem,InventoryItem&$filter=SalesItem eq 'tYES' and InventoryItem eq 'tYES'&$top=6")
        self.stdout.write(self.style.WARNING(f"\n== Items (sellable+inventory)  [{code}] =="))
        for it in (data.get("value") if isinstance(data, dict) else []) or []:
            self.stdout.write(
                f"  {it.get('ItemCode'):16} inUoM={it.get('InventoryUOM')!s:6} salesUoM={it.get('SalesUnit')!s:8} {it.get('ItemName')}"
            )

        # Sales tax codes (VatGroup)
        code, data = get("SalesTaxCodes?$select=Code,Name&$top=15")
        self.stdout.write(self.style.WARNING(f"\n== Sales tax codes (VatGroup)  [{code}] =="))
        for t in (data.get("value") if isinstance(data, dict) else []) or []:
            self.stdout.write(f"  {t.get('Code'):10} {t.get('Name')}")

        # Series for Delivery Notes (object 15)
        code, data = get("SeriesService_GetDocumentSeries")
        self.stdout.write(self.style.WARNING(f"\n== (Series endpoint probe)  [{code}] =="))
        self.stdout.write(f"  {str(data)[:200]}")

"""Seed demo marketplace data for the Mayapuri Flipkart/Amazon pilot.

Idempotent. Usage:  python manage.py seed_marketplace_demo --company JIVO_MART
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from marketplace.models import (
    ComboComponent,
    ComboComponentType,
    ComboDefinition,
    MarketplaceChannel,
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    MarketplaceWarehouse,
    SkuMapping,
    SkuType,
)


class Command(BaseCommand):
    help = "Seed demo marketplace masters + orders for a company."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_MART")

    def handle(self, *args, **opts):
        code = opts["company"]
        try:
            company = Company.objects.get(code=code)
        except Company.DoesNotExist:
            raise CommandError(f"Company '{code}' not found.")

        wh, _ = MarketplaceWarehouse.objects.get_or_create(
            company=company, channel=MarketplaceChannel.FLIPKART,
            sap_warehouse_code="FLP-MAY",
            defaults=dict(name="Mayapuri Flipkart Godown", sap_customer_card_code="C-FLIPKART",
                          facility_code="MAYAPURI"),
        )
        MarketplaceWarehouse.objects.get_or_create(
            company=company, channel=MarketplaceChannel.AMAZON,
            sap_warehouse_code="AMZ-MAY",
            defaults=dict(name="Mayapuri Amazon Godown", sap_customer_card_code="C-AMAZON",
                          facility_code="MAYAPURI"),
        )

        combo, _ = ComboDefinition.objects.get_or_create(
            company=company, channel=MarketplaceChannel.FLIPKART, code="COMBO-OIL-3L",
            defaults=dict(name="Jivo 3L Oil Combo"),
        )
        if not combo.components.exists():
            ComboComponent.objects.create(
                combo=combo, component_type=ComboComponentType.FG,
                item_code="FG-OIL-3L", item_name="Jivo Oil 3L Bottle", quantity=Decimal("1"), uom="EA")
            ComboComponent.objects.create(
                combo=combo, component_type=ComboComponentType.PM,
                item_code="PM-TAPE", item_name="Packing Tape", quantity=Decimal("2"), uom="M")

        SkuMapping.objects.get_or_create(
            company=company, channel=MarketplaceChannel.FLIPKART, marketplace_sku="FSN-OIL-1L",
            defaults=dict(sku_type=SkuType.RAW, fg_item_code="FG-OIL-1L",
                          fg_item_name="Jivo Oil 1L", default_uom="EA", sku_name="Jivo Oil 1L"),
        )
        SkuMapping.objects.get_or_create(
            company=company, channel=MarketplaceChannel.FLIPKART, marketplace_sku="FSN-OIL-3L-COMBO",
            defaults=dict(sku_type=SkuType.COMBO, combo=combo, sku_name="Jivo 3L Oil Combo"),
        )

        order, created = MarketplaceOrder.objects.get_or_create(
            company=company, channel=MarketplaceChannel.FLIPKART, order_id="OD-DEMO-1001",
            defaults=dict(buyer_name="Demo Buyer", sap_warehouse_code="FLP-MAY",
                          status=MarketplaceOrderStatus.OPEN),
        )
        if created:
            MarketplaceOrderLine.objects.create(order=order, marketplace_sku="FSN-OIL-1L",
                                                sku_name="Jivo Oil 1L", ordered_quantity=Decimal("2"))
            MarketplaceOrderLine.objects.create(order=order, marketplace_sku="FSN-OIL-3L-COMBO",
                                                sku_name="Jivo 3L Oil Combo", ordered_quantity=Decimal("3"))

        self.stdout.write(self.style.SUCCESS(
            f"Seeded marketplace demo for {code}: warehouse {wh.sap_warehouse_code}, "
            f"combo {combo.code}, order {order.order_id}."
        ))

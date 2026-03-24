"""
Service for syncing Sales Orders from SAP HANA into ShipmentOrder records.
"""
import logging
from typing import Dict, List, Any
from decimal import Decimal

from django.db import transaction

from company.models import Company
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

from ..models import ShipmentOrder, ShipmentOrderItem, ShipmentStatus

logger = logging.getLogger(__name__)


class SAPSyncService:
    """Syncs open Sales Orders from SAP into local ShipmentOrder records."""

    def __init__(self, company_code: str):
        self.company_code = company_code

    @transaction.atomic
    def sync_sales_orders(self, user=None, filters: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Sync open Sales Orders from SAP HANA.

        Returns:
            dict with created_count, updated_count, skipped_count, errors
        """
        company = Company.objects.get(code=self.company_code)
        sap_client = SAPClient(company_code=self.company_code)

        try:
            sap_orders = sap_client.get_open_sales_orders(filters)
        except (SAPConnectionError, SAPDataError) as e:
            logger.error(f"Failed to fetch Sales Orders from SAP: {e}")
            raise

        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []

        for sap_order in sap_orders:
            try:
                shipment, created = ShipmentOrder.objects.get_or_create(
                    company=company,
                    sap_doc_entry=sap_order["doc_entry"],
                    defaults={
                        "sap_doc_num": sap_order["doc_num"],
                        "customer_code": sap_order["customer_code"],
                        "customer_name": sap_order["customer_name"],
                        "ship_to_address": sap_order.get("ship_to_address", ""),
                        "scheduled_date": sap_order["due_date"],
                        "status": ShipmentStatus.RELEASED,
                        "notes": sap_order.get("comments", ""),
                        "created_by": user,
                    }
                )

                if created:
                    # Create line items
                    for item in sap_order["items"]:
                        ShipmentOrderItem.objects.create(
                            shipment_order=shipment,
                            sap_line_num=item["line_num"],
                            item_code=item["item_code"],
                            item_name=item["item_name"],
                            ordered_qty=Decimal(str(item["remaining_qty"])),
                            uom=item["uom"],
                            warehouse_code=item["warehouse_code"],
                            batch_number=item.get("batch_number", ""),
                            created_by=user,
                        )
                    created_count += 1
                    logger.info(f"Created ShipmentOrder for SO-{sap_order['doc_num']}")
                else:
                    # Skip already-synced orders that are beyond RELEASED status
                    if shipment.status != ShipmentStatus.RELEASED:
                        skipped_count += 1
                        continue

                    # Update existing RELEASED order with latest SAP data
                    shipment.customer_name = sap_order["customer_name"]
                    shipment.ship_to_address = sap_order.get("ship_to_address", "")
                    shipment.scheduled_date = sap_order["due_date"]
                    shipment.save()

                    # Update/create items
                    existing_lines = set(
                        shipment.items.values_list("sap_line_num", flat=True)
                    )
                    for item in sap_order["items"]:
                        if item["line_num"] in existing_lines:
                            ShipmentOrderItem.objects.filter(
                                shipment_order=shipment,
                                sap_line_num=item["line_num"]
                            ).update(
                                ordered_qty=Decimal(str(item["remaining_qty"])),
                                item_name=item["item_name"],
                            )
                        else:
                            ShipmentOrderItem.objects.create(
                                shipment_order=shipment,
                                sap_line_num=item["line_num"],
                                item_code=item["item_code"],
                                item_name=item["item_name"],
                                ordered_qty=Decimal(str(item["remaining_qty"])),
                                uom=item["uom"],
                                warehouse_code=item["warehouse_code"],
                                batch_number=item.get("batch_number", ""),
                                created_by=user,
                            )
                    updated_count += 1

            except Exception as e:
                error_msg = f"Error syncing SO-{sap_order.get('doc_num')}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)

        result = {
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "total_from_sap": len(sap_orders),
            "errors": errors,
        }
        logger.info(f"SAP Sales Order sync complete: {result}")
        return result

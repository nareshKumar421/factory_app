"""
warehouse/models.py

Permission-only model for Phase 1 (read-only from SAP HANA).
No database tables are created — this registers custom permissions
for the warehouse module.
"""

from django.db import models


class WarehousePermission(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            # Dashboard
            ("can_view_warehouse_dashboard", "Can view warehouse dashboard"),
            # Inventory
            ("can_view_inventory", "Can view inventory"),
            # Inward / Putaway
            ("can_view_inward", "Can view inward receipts"),
            ("can_execute_inward", "Can execute inward putaway"),
            # Stock Transfers
            ("can_view_transfers", "Can view stock transfers"),
            ("can_request_transfer", "Can request stock transfer"),
            ("can_approve_transfer", "Can approve stock transfer"),
            ("can_pick_transfer", "Can pick stock transfer"),
            ("can_receive_transfer", "Can receive stock transfer"),
            ("can_post_transfer_to_sap", "Can post stock transfer to SAP"),
            # Goods Issue
            ("can_view_goods_issue", "Can view goods issues"),
            ("can_request_goods_issue", "Can request goods issue"),
            ("can_approve_goods_issue", "Can approve goods issue"),
            ("can_execute_goods_issue", "Can execute goods issue"),
            ("can_post_goods_issue_to_sap", "Can post goods issue to SAP"),
            # Stock Counting
            ("can_view_stock_count", "Can view stock counts"),
            ("can_plan_stock_count", "Can plan stock count"),
            ("can_execute_stock_count", "Can execute stock count"),
            ("can_review_stock_count", "Can review stock count"),
            ("can_post_stock_count_to_sap", "Can post stock count to SAP"),
            # Picking
            ("can_view_picking", "Can view pick lists"),
            ("can_execute_picking", "Can execute picking"),
            ("can_manage_picking", "Can manage pick lists"),
            # Dispatch Tracking
            ("can_view_dispatch_tracking", "Can view dispatch tracking"),
            ("can_manage_dispatch_tracking", "Can manage dispatch tracking"),
            # Returns
            ("can_view_returns", "Can view returns"),
            ("can_create_return", "Can create return"),
            ("can_inspect_return", "Can inspect return"),
            ("can_complete_return", "Can complete return"),
            # Non-Moving / Expiry
            ("can_view_non_moving", "Can view non-moving report"),
            ("can_manage_non_moving_config", "Can manage expiry threshold config"),
            # Capacity Limits
            ("can_view_capacity_limits", "Can view capacity limits"),
            ("can_manage_capacity_limits", "Can manage capacity limits"),
            # Daily Audit
            ("can_view_audit", "Can view daily audits"),
            ("can_execute_audit", "Can execute daily audit"),
        ]

"""
production_movements/constants.py

Warehouse-role vocabulary and the per-company seed for the production
material-movement flow (GRPO -> PM stores -> issue point -> production -> BH-PF).

Scope (locked 2026-07-28): PM-only, companies JIVO_OIL + JIVO_BEVERAGES.
Mart is excluded (it does not manufacture). RM is out of scope for now.

SAP item groups (authoritative): 106=RAW MATERIAL, 105=PACKAGING MATERIAL,
102=FINISHED. This module currently drives PACKAGING (105) only.
"""


class WarehouseRoleType:
    """The role a physical/virtual SAP warehouse plays in the production flow."""

    RM_STORE = "RM_STORE"                      # raw-material store (out of scope for now)
    PM_STORE = "PM_STORE"                      # packaging-material store (feeds the issue point)
    PRODUCTION_CONSUMPTION = "PRODUCTION_CONSUMPTION"  # the floor godown BOM is issued FROM
    FG_RECEIPT = "FG_RECEIPT"                  # finished-goods land here from the production order
    GR_STAGING = "GR_STAGING"                  # goods-receipt staging
    WASTAGE = "WASTAGE"                        # wastage/scrap
    VIRTUAL = "VIRTUAL"                        # virtual/consolidation warehouse (needs resolution)
    INACTIVE = "INACTIVE"                      # not used in the flow (cleanup flag)
    OTHER = "OTHER"

    CHOICES = [
        (RM_STORE, "RM Store"),
        (PM_STORE, "PM Store"),
        (PRODUCTION_CONSUMPTION, "Production Consumption (BOM issue point)"),
        (FG_RECEIPT, "FG Receipt (from production)"),
        (GR_STAGING, "GR Staging"),
        (WASTAGE, "Wastage"),
        (VIRTUAL, "Virtual / Consolidation"),
        (INACTIVE, "Inactive (cleanup)"),
        (OTHER, "Other"),
    ]


class ItemFamily:
    """Which SAP item family a warehouse predominantly holds."""

    RM = "RM"
    PM = "PM"
    FG = "FG"
    MIXED = "MIXED"
    OTHER = "OTHER"

    CHOICES = [
        (RM, "Raw Material"),
        (PM, "Packaging Material"),
        (FG, "Finished Goods"),
        (MIXED, "Mixed"),
        (OTHER, "Other"),
    ]


# SAP item-group codes this module cares about.
ITEM_GROUP_PM = 105
ITEM_GROUP_RM = 106
ITEM_GROUP_FG = 102

# SAP item-group NAME as it appears in OITB.ItmsGrpNam, used by the stock
# reader's `item_group` filter. Verified against JIVO_OIL/JIVO_BEVERAGES HANA.
ITEM_GROUP_PM_NAME = "PACKAGING MATERIAL"


# ---------------------------------------------------------------------------
# Per-company seed. Live-verified against HANA on 2026-07-28.
#   role fields: (whs_code, role, family, is_grpo_target, is_bom_issue_point,
#                 feeds_whs_code, transfer_needs_request, needs_review, notes)
#
# transfer_needs_request encodes SP rule 67081: a transfer INTO BH-PC from
# BH-PM/BH-LO is rejected unless based on an Inventory Transfer Request. It fires
# ONLY for BH-PC (Oil). BH-BS->BH-PC is exempt, and Beverages (issue point BH-PP)
# is not covered by the rule at all -> all False there.
# ---------------------------------------------------------------------------
WAREHOUSE_ROLE_SEED = {
    "JIVO_OIL": [
        # PM stores feed the production-consumption godown BH-PC.
        dict(whs_code="BH-PM", role=WarehouseRoleType.PM_STORE, family=ItemFamily.PM,
             is_grpo_target=True, is_bom_issue_point=False, feeds_whs_code="BH-PC",
             transfer_needs_request=True, needs_review=False,
             notes="Primary PM store. GRPO for packaging is received here (5,579 recent PDN1 lines). "
                   "BH-PM->BH-PC needs an Inventory Transfer Request (SP 67081)."),
        dict(whs_code="BH-BS", role=WarehouseRoleType.PM_STORE, family=ItemFamily.PM,
             is_grpo_target=False, is_bom_issue_point=False, feeds_whs_code="BH-PC",
             transfer_needs_request=False, needs_review=False,
             notes="Secondary PM store (Basement). Feeds BH-PC. Exempt from the ITR rule."),
        # The single BOM issue point.
        dict(whs_code="BH-PC", role=WarehouseRoleType.PRODUCTION_CONSUMPTION, family=ItemFamily.PM,
             is_grpo_target=False, is_bom_issue_point=True, feeds_whs_code="",
             transfer_needs_request=False, needs_review=False,
             notes="Production-consumption godown on the floor. BOM (PM) is issued ONLY from here."),
        # Finished goods boundary. After this, BST + dispatch take over.
        dict(whs_code="BH-PF", role=WarehouseRoleType.FG_RECEIPT, family=ItemFamily.FG,
             is_grpo_target=False, is_bom_issue_point=False, feeds_whs_code="",
             transfer_needs_request=False, needs_review=False,
             notes="Production-finished receipt whs (OWOR header, 6,124 orders). Flow boundary."),
        # Not used in the active flow; kept as a cleanup marker.
        dict(whs_code="BH-PP", role=WarehouseRoleType.INACTIVE, family=ItemFamily.PM,
             is_grpo_target=False, is_bom_issue_point=False, feeds_whs_code="",
             transfer_needs_request=False, needs_review=True,
             notes="NOT USED in Oil flow (user-confirmed). Holds legacy PM on-hand -> consolidate/cleanup."),
    ],
    "JIVO_BEVERAGES": [
        # Beverages has NO BH-PC; its issue point is BH-PP. BH-BS is inactive here.
        dict(whs_code="BH-PM", role=WarehouseRoleType.PM_STORE, family=ItemFamily.PM,
             is_grpo_target=True, is_bom_issue_point=False, feeds_whs_code="BH-PP",
             transfer_needs_request=False, needs_review=True,
             notes="Primary PM store + GRPO target (643 recent PDN1 lines). Review vs BH-VG. "
                   "BH-PP not covered by SP 67081 -> plain transfer."),
        dict(whs_code="BH-VG", role=WarehouseRoleType.VIRTUAL, family=ItemFamily.PM,
             is_grpo_target=False, is_bom_issue_point=False, feeds_whs_code="BH-PP",
             transfer_needs_request=False, needs_review=True,
             notes="Virtual Gopi godown, mixes PM+RM. RESOLVE placement before enabling writes."),
        dict(whs_code="BH-PP", role=WarehouseRoleType.PRODUCTION_CONSUMPTION, family=ItemFamily.PM,
             is_grpo_target=False, is_bom_issue_point=True, feeds_whs_code="",
             transfer_needs_request=False, needs_review=True,
             notes="Beverages BOM issue point (WOR1: 5,562 PM lines). No BH-PC exists here."),
        dict(whs_code="BH-PF", role=WarehouseRoleType.FG_RECEIPT, family=ItemFamily.FG,
             is_grpo_target=False, is_bom_issue_point=False, feeds_whs_code="",
             transfer_needs_request=False, needs_review=True,
             notes="Production-finished receipt whs (OWOR header, 1,171 orders). Flow boundary."),
    ],
}

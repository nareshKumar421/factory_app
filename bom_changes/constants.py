"""Choices and SAP vocabularies for BOM change requests.

The status values are SAP Portal's own (``backend_v1/server.js`` ``STATUS_FLOW``,
lines 28-32), so an imported portal row keeps the status it had. The portal also
listed ``DRAFT`` but never wrote it (every insert is ``PENDING``,
``services/bomRequestStore.js`` ``insertBomRequest``), so it is not here.
"""

from django.db import models


class BOMChangeKind(models.TextChoices):
    CREATE = "CREATE", "New BOM"
    UPDATE = "UPDATE", "Change to a BOM"


class BOMChangeStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    L1_APPROVED = "L1_APPROVED", "Level 1 approved"
    L2_APPROVED = "L2_APPROVED", "Level 2 approved"
    # Only reached with four levels: the first of the two SAP pushers signed.
    L3_APPROVED = "L3_APPROVED", "Level 3 approved"
    SAP_PUSHED = "SAP_PUSHED", "In SAP"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


#: Statuses a request can still move from, in order. The index is the number of
#: approvals the request already has.
OPEN_STATUSES = (
    BOMChangeStatus.PENDING,
    BOMChangeStatus.L1_APPROVED,
    BOMChangeStatus.L2_APPROVED,
    BOMChangeStatus.L3_APPROVED,
)


class BOMType(models.TextChoices):
    """The portal's BOM types (``public/bom.html`` ``BOM_TYPES``), spelled as it stored them."""

    PRODUCTION = "Production", "Production"
    SALES = "Sales", "Sales"
    ASSEMBLY = "Assembly", "Assembly"
    TEMPLATE = "Template", "Template"


#: BOM type → SAP ``ProductTrees.TreeType`` (server.js ``TREE_TYPE_MAP``, lines 296-299).
#: The portal fell back to a production tree for anything it did not know.
TREE_TYPE_MAP = {
    "production": "iProductionTree",
    "sales": "iSalesTree",
    "assembly": "iAssemblyTree",
    "template": "iTemplateTree",
    "disassembly": "iDisassemblyTree",
}
DEFAULT_TREE_TYPE = "iProductionTree"

#: ``OITT.TreeType`` as HANA stores it → our BOM type.
HANA_TREE_TYPES = {
    "P": BOMType.PRODUCTION,
    "S": BOMType.SALES,
    "A": BOMType.ASSEMBLY,
    "T": BOMType.TEMPLATE,
}


class IssueMethod(models.TextChoices):
    MANUAL = "Manual", "Manual"
    BACKFLUSH = "Backflush", "Backflush"


#: Issue method → SAP ``IssueMethod`` (server.js ``ISSUE_METHOD_MAP``, lines 300-303). The
#: portal's page offered only Manual and Backflush; the aliases are kept so an
#: imported component that said "Stock" or "Phantom" maps the way the portal pushed it.
ISSUE_METHOD_MAP = {
    "Manual": "im_Manual",
    "Backflush": "im_Backflush",
    "Stock": "im_Backflush",
    "Non-Stock": "im_Manual",
    "Phantom": "im_Manual",
    "Fixed": "im_Backflush",
}
DEFAULT_ISSUE_METHOD = "im_Manual"


class LineType(models.TextChoices):
    ITEM = "item", "Item"
    RESOURCE = "resource", "Resource"


#: Line type → SAP ``ProductTreeLines.ItemType``.
SAP_ITEM_TYPES = {LineType.ITEM: "pit_Item", LineType.RESOURCE: "pit_Resource"}


class ApprovalAction(models.TextChoices):
    APPROVE = "APPROVE", "Approved"
    REJECT = "REJECT", "Rejected"


#: ``BOMChangeApproval.level`` of a push that skipped the approval levels
#: (the portal admin's direct create/update).
DIRECT_LEVEL = 0

#: ``ProductTrees.ProductDescription`` and a line ``Comment`` are cut to this by
#: the portal (server.js lines 495, 498, 509). A comment is refused past it here
#: instead; the description, copied from the item name, is cut as the portal did.
SAP_TEXT_LIMIT = 100

"""Names the Fixed Asset GRPO app shares between its modules.

Kept in a module that imports nothing, so ``apps.py`` can read the queue kind
in ``ready()`` without pulling in models, services or SAP clients.
"""

#: The gate entry type this app posts. A GRPO of any other type is refused at
#: every door (draft save, post, queue handler).
FA_ENTRY_TYPE = "FIXED_ASSET"

#: The SAP posting queue kind (``sap_postings``). Our own kind, not
#: ``grpo.material``: the material handler cannot be edited, and it knows
#: nothing of the switch, simulate or the live-SAP guard.
KIND = "fixed_asset_grpo.post"
HANDLER_PATH = "fixed_asset_grpo.sap_posting.FixedAssetGRPOHandler"
KIND_LABEL = "Fixed asset GRPO"
KIND_SHORT = "fixed-asset GRPOs"

#: Permissions, as ``user.has_perm`` wants them. All three are declared on
#: ``FixedAssetGRPORecord.Meta``.
VIEW_PERMISSION = "fixed_asset_grpo.can_view_fixed_asset_grpo"
POST_PERMISSION = "fixed_asset_grpo.can_post_fixed_asset_grpo"
LINK_PERMISSION = "fixed_asset_grpo.can_link_fixed_asset_po"

#: The auth group the data migration creates and notifications go to.
FA_GRPO_GROUP = "Fixed Asset GRPO"

#: Where the FactoryFlow screens live. Links on queue rows and notifications
#: are built from this one prefix, so a route change is a one-line change.
FRONTEND_BASE = "/warehouse/grpo/fixed-asset"

#: The material queue kind, whose live rows also count as "on its way": a GRPO
#: of an FA entry posted through grpo's generic draft endpoint (the known
#: residual, see docs/README.md) must still block a second send from here.
MATERIAL_KIND = "grpo.material"

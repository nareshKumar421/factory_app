"""Write bills of materials (``ProductTrees``) to SAP.

Ported from SAP Portal's ``pushBomToSap`` (``backend_v1/server.js``): a new BOM
is a POST, a change to an existing one a PUT that replaces the whole tree, so
lines removed in the request disappear from SAP too. The payload is built by
``bom_changes.services``.

The tree code is quoted as an OData literal. The portal put it between quotes
unescaped (``ProductTrees('<code>')``), so an item code holding a quote broke
the key.
"""

import logging

from .entity_client import ServiceLayerEntityClient, odata_string

logger = logging.getLogger(__name__)

POST_TIMEOUT_SECONDS = 120


class ProductTreeWriter:
    """POST / PUT /b1s/v2/ProductTrees."""

    def __init__(self, context):
        self.context = context

    def create(self, payload: dict) -> dict:
        tree_code = payload.get("TreeCode")
        data = ServiceLayerEntityClient(self.context).post(
            "ProductTrees",
            payload,
            timeout=POST_TIMEOUT_SECONDS,
            label=f"create the BOM for {tree_code}",
        )
        logger.info("BOM %s created in SAP", tree_code)
        return {"tree_code": data.get("TreeCode") or tree_code}

    def replace(self, tree_code: str, payload: dict) -> dict:
        ServiceLayerEntityClient(self.context).put(
            f"ProductTrees({odata_string(tree_code)})",
            payload,
            timeout=POST_TIMEOUT_SECONDS,
            label=f"update the BOM for {tree_code}",
        )
        logger.info("BOM %s replaced in SAP", tree_code)
        return {"tree_code": tree_code}

"""Warehouse Inventory endpoint: oil in SAP's warehouses, in litres."""

from rest_framework.response import Response

from . import services_inventory
from .permissions import Rights
from .views_tank import _Base, _company


class WarehouseInventoryAPI(_Base):
    """GET [?warehouse=<code>] [&refresh=1] : litres of oil by warehouse and
    category (RM and FG); with ``warehouse``, that warehouse's items."""

    rights = {"GET": (Rights.INVENTORY_VIEW,)}

    def get(self, request):
        from sap_client.exceptions import SAPConnectionError, SAPDataError

        from .services_tank import SapUnavailable

        params = request.query_params
        try:
            return Response(services_inventory.warehouse_inventory(
                _company(request), warehouse=params.get("warehouse", "").strip(),
                refresh=params.get("refresh") in ("1", "true"),
            ))
        except (SAPConnectionError, SAPDataError) as exc:
            raise SapUnavailable("SAP is not answering, so the stock cannot be read now. Try again shortly.",
                                 "sap_unavailable", {}) from exc

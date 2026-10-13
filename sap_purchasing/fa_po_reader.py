"""Open fixed-asset PO lines for one supplier: the Fixed Asset GRPO PO picker.

A fixed asset bought from a vendor is ordered on a normal item PO whose lines
sit in a fixed-asset warehouse (``BH-FA``, ``DL-FA``, ``GP-FA``). The FA GRPO
screen links a completed FA gate entry to such lines ("late link"), saves them
as ``POReceipt``/``POItemReceipt`` rows the way finished-goods receipts are
saved, and the GRPO engine posts against them.

``HanaFixedAssetPOReader`` subclasses ``sap_client.hana.po_reader.HanaPOReader``
-- whose source may not change -- and adds ``get_open_fixed_asset_pos``, the
mirror of ``get_open_finished_goods_pos``:

* **Live query.** The reader's own ``OPEN_LINE_COLUMNS`` (so rows go through
  the same ``_transform_to_dtos`` and come back as the same ``PODTO`` /
  ``POItemDTO``), plus ``T0."DocType" = 'I'``, ``T1."ItemCode" IS NOT NULL``
  and ``T1."WhsCode" IN (?, ...)``, one placeholder per warehouse. The
  DocType/ItemCode filter matters: a service PO line has no item, and
  ``POItemReceipt.po_item_code`` is NOT NULL, so one would crash the save.
* **Copy fallback.** When HANA cannot be reached the reader's ``_from_copy``
  answers from ``sap_mirror``'s copy of open PO lines. That copy's
  ``open_po_rows`` cannot filter by warehouse (it may not change either), so
  the rows are filtered here: warehouse (column 13) in the list and item code
  (column 3) not blank. The copy keeps no ``DocType``; a blank item code is
  what a service line looks like there.

The warehouse list comes from ``FIXED_ASSET_WAREHOUSES`` (via ``getattr``;
``settings.py`` is not edited), default ``"BH-FA,DL-FA,GP-FA"``. It may be a
comma-separated string or a list (``decouple.Csv`` gives a list).
"""

import logging
from typing import List, Optional, Sequence

from django.conf import settings

from sap_client.context import CompanyContext
from sap_client.dtos import PODTO
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.po_reader import OPEN_LINE_COLUMNS, HanaPOReader

logger = logging.getLogger(__name__)

DEFAULT_FIXED_ASSET_WAREHOUSES = "BH-FA,DL-FA,GP-FA"

#: Column positions in an open-line row (``OPEN_LINE_COLUMNS`` order).
ITEM_CODE_COL = 3
WAREHOUSE_COL = 13


def _clean(warehouses) -> List[str]:
    if isinstance(warehouses, str):
        warehouses = warehouses.split(",")
    seen = []
    for whs in warehouses or ():
        whs = str(whs or "").strip()
        if whs and whs not in seen:
            seen.append(whs)
    return seen


def fixed_asset_warehouses() -> List[str]:
    """The configured fixed-asset warehouse codes, cleaned and de-duplicated."""
    return _clean(getattr(settings, "FIXED_ASSET_WAREHOUSES", DEFAULT_FIXED_ASSET_WAREHOUSES))


class HanaFixedAssetPOReader(HanaPOReader):
    """``HanaPOReader`` plus open fixed-asset PO lines. Build with a ``CompanyContext``."""

    def get_open_fixed_asset_pos(self, supplier_code: str,
                                 warehouses: Optional[Sequence[str]] = None) -> List[PODTO]:
        """Open item lines of ``supplier_code``'s POs in the given warehouses.

        ``warehouses`` defaults to ``fixed_asset_warehouses()``; an empty list
        raises ``ValueError`` (an empty ``IN ()`` is not SQL, and "every
        warehouse" is not what anyone means). Falls back to the copy when HANA
        is unreachable, exactly as ``get_open_finished_goods_pos`` does.
        """
        whs = _clean(fixed_asset_warehouses() if warehouses is None else warehouses)
        if not whs:
            raise ValueError("No fixed-asset warehouses are configured (FIXED_ASSET_WAREHOUSES).")
        try:
            return self._open_fixed_asset_pos_live(supplier_code, whs)
        except (SAPConnectionError, SAPDataError) as exc:
            rows = self._from_copy(exc, supplier_code=supplier_code)
            return self._transform_to_dtos(filter_copy_rows(rows, whs))

    def _open_fixed_asset_pos_live(self, supplier_code: str, warehouses: Sequence[str]) -> List[PODTO]:
        if not warehouses:
            raise ValueError("At least one warehouse is needed.")
        schema = self.connection.schema
        placeholders = ", ".join("?" for _ in warehouses)
        query = f"""
            SELECT {OPEN_LINE_COLUMNS}
            FROM "{schema}"."OPOR" T0
            JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry"
            WHERE T0."CardCode" = ?
              AND T1."OpenQty" > 0
              AND T0."DocType" = 'I'
              AND T1."ItemCode" IS NOT NULL
              AND T1."WhsCode" IN ({placeholders})
            ORDER BY T0."DocEntry", T1."LineNum"
            """
        # ``_fetch``: connect failure -> SAPConnectionError, query failure ->
        # SAPDataError, both chained to the driver error so the copy fallback
        # (``hana_unreachable``) can tell "could not ask" from "refused".
        rows = self._fetch(query, [supplier_code, *warehouses])
        return self._transform_to_dtos(rows)


def filter_copy_rows(rows, warehouses: Sequence[str]) -> list:
    """Keep copy rows in ``warehouses`` with a non-blank item code (pure)."""
    allowed = set(_clean(warehouses))
    return [
        row for row in rows or []
        if (row[WAREHOUSE_COL] or "") in allowed and str(row[ITEM_CODE_COL] or "").strip()
    ]


def get_open_fixed_asset_pos(company_code: str, supplier_code: str,
                             warehouses: Optional[Sequence[str]] = None) -> List[PODTO]:
    """Open fixed-asset POs of ``supplier_code`` in ``company_code`` as ``PODTO``s.

    The same DTOs ``SAPClient.get_open_pos`` / ``get_open_finished_goods_pos``
    return, so the FG-style receipt saver and ``POSerializer`` take them as is.
    """
    reader = HanaFixedAssetPOReader(CompanyContext(company_code))
    return reader.get_open_fixed_asset_pos(supplier_code, warehouses)

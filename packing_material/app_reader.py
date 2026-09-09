"""
packing_material/app_reader.py

The dispatch section, read from FactoryFlow's own gate-out register instead of
from SAP.

WHAT THIS SOURCE IS
-------------------
The bills that physically went out through docking. A ``SalesDispatchGateOut``
is one truck; the bills on it are ``SalesDispatchGateOutDocument`` rows and the
SKUs are ``SalesDispatchGateOutItem`` rows, each pointing at the one bill it
was invoiced on. So "which bills were dispatched, and what packing material
went out inside them" is answerable straight off these three tables -- which is
the whole reason this source exists alongside SAP.

WHY IT DOES NOT AGREE WITH SAP, AND SHOULD NOT
----------------------------------------------
Measured on the live database for Oil, August 2026:

    gate-outs with status DISPATCHED                125
    bills on them                                   396
    item lines                                      962
    SAP invoices carrying finished goods            603

396 of 603 invoices (66%) have a FactoryFlow docking behind them. The rest were
invoiced in SAP without passing one. The gap is not an error in either system
and this reader does not try to close it: the API reports which source answered
and how many bills it saw, and the two figures sit side by side under the
toggle.

ONLY TRUCKS THAT LEFT
---------------------
``status = DISPATCHED`` only. The register also holds DOCKED, PHOTO_ATTACHED,
READY_FOR_GATEPASS, GATEPASS_PRINTED and PRINT_COMMITTED -- loads still on the
premises -- plus REJECTED and CANCELLED. Counting those as dispatch would put
packaging on this board that is still sitting in the yard.

DATED BY THE DAY THE TRUCK LEFT
-------------------------------
``gate_out_date``, not ``created_at`` and not ``sap_doc_date``. The first is
when the row was typed, the last is when Accounts raised the bill; neither is
the day the packaging left the site. Every DISPATCHED row on the live database
carries a ``gate_out_date``, so nothing is dropped by keying on it.

QUANTITY IS IN PIECES
---------------------
``quantity`` is the billed piece count, verified line for line against
``INV1.Quantity`` on bill 626080610 -- 140, 3900, 130, 500 and 400 pieces,
agreeing exactly. ``dispatched_quantity`` is the partial-dispatch override and
is NULL on every August row, so it is read through a COALESCE: where somebody
has recorded a short load, the short figure is what left, and where they have
not, the billed figure is.

WHAT ALWAYS COMES FROM SAP, WHATEVER THE TOGGLE SAYS
----------------------------------------------------
**The recipe.** A bill of material is a master record living in
``OITT``/``ITT1``. Both sources explode through it.

**Which item codes are packing material and which are finished goods.**
``OITM.ItmsGrpCod`` is the only authority. Gate-out lines carry an item code
and no item group, and 18 of the distinct items on August's lines were
packaging rather than finished goods, so the group has to be looked up before
anything can be exploded.
"""

import logging
from typing import Any, Dict, List

from django.db.models import Count, F, Sum
from django.db.models.functions import Coalesce

logger = logging.getLogger(__name__)


class PackingMaterialAppReader:
    """Reads FactoryFlow's gate-out register for one company."""

    def __init__(self, company_code: str):
        self.company_code = company_code

    def _dispatched(self, date_from, date_to):
        from gate_core.models import SalesDispatchGateOut, SalesDispatchGateOutStatus

        return SalesDispatchGateOut.objects.filter(
            company__code=self.company_code,
            status=SalesDispatchGateOutStatus.DISPATCHED,
            gate_out_date__gte=date_from,
            gate_out_date__lte=date_to,
        )

    def dispatched_lines(self, date_from, date_to) -> List[Dict[str, Any]]:
        """Items that went out through the gate, per item, in pieces.

        No intercompany split and no credit-note netting: the register records
        a truck leaving, not who was invoiced, and a sales return comes back
        through the returns module rather than as a negative gate-out line.
        Both figures stay zero here and the response says they are SAP-only.
        """
        from gate_core.models import SalesDispatchGateOutItem

        rows = (
            SalesDispatchGateOutItem.objects.filter(
                sales_dispatch__in=self._dispatched(date_from, date_to)
            )
            .values("item_code", "item_name")
            .annotate(
                qty=Sum(Coalesce("dispatched_quantity", F("quantity"))),
                lines=Count("id"),
            )
        )

        return [
            {
                "item_code": (row["item_code"] or "").strip(),
                "item_name": row["item_name"] or "",
                "qty": float(row["qty"] or 0),
                "line_count": int(row["lines"] or 0),
                "intercompany_qty": 0.0,
                "return_qty": 0.0,
            }
            for row in rows
            if (row["item_code"] or "").strip() and row["qty"]
        ]

    def dispatch_counts(self, date_from, date_to) -> Dict[str, int]:
        """How many trucks left, and how many bills they carried.

        The bills are counted on ``SalesDispatchGateOutDocument`` and NOT on
        the gate-out header. One truck routinely carries several bills -- 125
        trucks carried 396 in August -- and the header's ``sap_doc_num`` holds
        them as one comma-joined string ("626080610, 626080611"), so counting
        distinct header values reports trucks under the name of bills.
        """
        from gate_core.models import SalesDispatchGateOutDocument

        gate_outs = self._dispatched(date_from, date_to)
        return {
            "gate_out_count": gate_outs.count(),
            "document_count": SalesDispatchGateOutDocument.objects.filter(
                sales_dispatch__in=gate_outs
            ).count(),
        }

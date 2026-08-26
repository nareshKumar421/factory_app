"""Batch availability and batch-allocation verification for stock transfers.

Two tables, two jobs:

* `OIBT` — how much of each batch sits in a warehouse right now. This is what a
  transfer line allocates against.
* `IBT1` — what a posted document actually allocated. A transfer writes paired
  rows per batch: `Direction` 1 out of the source, `Direction` 0 into the
  destination.

Reads must come from here, not from the Service Layer: a `GET` on a document
returns `BatchNumbers: []` even for documents that *do* carry batches (verified
against a live document that has two batch splits in `IBT1`). Only the POST
response echoes them, so any verification that trusts a GET reports every batch
transfer as unallocated.
"""

import logging
from decimal import Decimal
from typing import Iterable, Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# SAP IBT1."Direction"
DIRECTION_IN = 0
DIRECTION_OUT = 1

# SAP object code for an inventory transfer
BASE_TYPE_STOCK_TRANSFER = 67


class InsufficientBatchStock(SAPDataError):
    """Raised when the requested quantity exceeds what the batches hold."""


class HanaBatchStockReader:
    """Read batch stock (OIBT) and posted batch allocations (IBT1)."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # item master
    # ------------------------------------------------------------------

    def batch_managed_flags(self, item_codes: Iterable[str]) -> dict[str, bool]:
        """Map item code -> whether SAP requires batch allocation for it.

        Batch management is per item, not per item group: in Oil, 464 of 466
        finished goods and 79 of 83 raw materials are batch-managed, but only 32
        of 875 packaging items are. So this has to be asked per item rather than
        inferred from the code prefix.
        """
        codes = [c for c in {str(c) for c in item_codes} if c]
        if not codes:
            return {}

        placeholders = ", ".join(["?"] * len(codes))
        rows = self._query(
            f"""
            SELECT "ItemCode", IFNULL("ManBtchNum", 'N')
            FROM "{{schema}}"."OITM"
            WHERE "ItemCode" IN ({placeholders})
            """,
            tuple(codes),
        )
        return {row[0]: (row[1] == "Y") for row in rows}

    # ------------------------------------------------------------------
    # availability
    # ------------------------------------------------------------------

    def available_batches(self, item_code: str, warehouse: str) -> list[dict]:
        """Batches of `item_code` holding stock in `warehouse`, oldest first."""
        rows = self._query(
            """
            SELECT
                B."BatchNum",
                B."Quantity",
                IFNULL(B."Status", '0'),
                TO_DATE(B."InDate"),
                TO_DATE(B."ExpDate"),
                TO_DATE(B."PrdDate"),
                B."SysNumber"
            FROM "{schema}"."OIBT" B
            WHERE B."ItemCode" = ?
              AND B."WhsCode" = ?
              AND B."Quantity" > 0
            ORDER BY B."InDate" ASC, B."BatchNum" ASC
            """,
            (str(item_code), str(warehouse)),
        )
        return [
            {
                "batch_number": row[0] or "",
                "quantity": Decimal(str(row[1] or 0)),
                "status": row[2] or "0",
                "in_date": row[3],
                "expiry_date": row[4],
                "production_date": row[5],
                "system_number": int(row[6]) if row[6] is not None else None,
            }
            for row in rows
        ]

    def allocate_fifo(
        self,
        item_code: str,
        warehouse: str,
        quantity,
        *,
        released_only: bool = True,
    ) -> list[dict]:
        """Split `quantity` across the oldest batches first.

        Returns the `BatchNumbers` payload a transfer line needs:
        `[{"BatchNumber": ..., "Quantity": ...}, ...]` with the caller adding
        `BaseLineNumber`. Raises InsufficientBatchStock rather than allocating
        short, so a shortfall surfaces before the POST instead of as an opaque
        SAP rejection.
        """
        wanted = Decimal(str(quantity))
        if wanted <= 0:
            raise SAPDataError("Batch allocation needs a positive quantity.")

        batches = self.available_batches(item_code, warehouse)
        if released_only:
            # OIBT."Status" 0 = released; anything else is locked for use.
            batches = [b for b in batches if b["status"] == "0"]

        allocation: list[dict] = []
        remaining = wanted
        for batch in batches:
            if remaining <= 0:
                break
            take = min(remaining, batch["quantity"])
            if take <= 0:
                continue
            allocation.append(
                {"BatchNumber": batch["batch_number"], "Quantity": float(take)}
            )
            remaining -= take

        if remaining > 0:
            held = sum((b["quantity"] for b in batches), Decimal("0"))
            raise InsufficientBatchStock(
                f"{item_code} has only {held} in {warehouse} across "
                f"{len(batches)} released batch(es); {wanted} was requested."
            )

        return allocation

    def check_allocation(
        self,
        item_code: str,
        warehouse: str,
        batches: list[dict],
        *,
        released_only: bool = True,
    ) -> list[dict]:
        """Validate a hand-picked batch split against what the warehouse holds.

        FIFO allocation can't pick a batch that isn't there, but a split the
        operator typed can — a batch that has moved on, or more of one than it
        holds. Raises rather than letting SAP reject the whole post with a
        generic allocation error.

        Returns the split normalised to `[{"BatchNumber", "Quantity"}]`.
        """
        if not batches:
            raise SAPDataError(f"No batches were chosen for {item_code}.")

        held = {
            b["batch_number"]: b
            for b in self.available_batches(item_code, warehouse)
            if not released_only or b["status"] == "0"
        }

        normalised: list[dict] = []
        wanted_per_batch: dict[str, Decimal] = {}

        for entry in batches:
            number = str(entry.get("BatchNumber") or entry.get("batch_number") or "").strip()
            quantity = Decimal(str(entry.get("Quantity") or entry.get("quantity") or 0))

            if not number:
                raise SAPDataError(f"A batch line for {item_code} has no batch number.")
            if quantity <= 0:
                raise SAPDataError(
                    f"Batch {number} of {item_code} was given a quantity of "
                    f"{quantity}; it must be positive."
                )
            if number not in held:
                raise InsufficientBatchStock(
                    f"Batch {number} is not available for {item_code} in "
                    f"{warehouse}. It may have already moved."
                )

            wanted_per_batch[number] = wanted_per_batch.get(number, Decimal("0")) + quantity
            normalised.append({"BatchNumber": number, "Quantity": float(quantity)})

        for number, wanted in wanted_per_batch.items():
            available = held[number]["quantity"]
            if wanted > available:
                raise InsufficientBatchStock(
                    f"Batch {number} of {item_code} holds {available} in "
                    f"{warehouse}, but {wanted} was asked for."
                )

        return normalised

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    def posted_allocations(
        self, doc_entry: int, *, base_type: int = BASE_TYPE_STOCK_TRANSFER
    ) -> list[dict]:
        """Batch rows SAP actually wrote for a posted document."""
        rows = self._query(
            """
            SELECT
                B."BaseLinNum",
                B."ItemCode",
                B."BatchNum",
                B."WhsCode",
                B."Quantity",
                B."Direction"
            FROM "{schema}"."IBT1" B
            WHERE B."BaseType" = ? AND B."BaseEntry" = ?
            ORDER BY B."BaseLinNum", B."Direction", B."BatchNum"
            """,
            (int(base_type), int(doc_entry)),
        )
        return [
            {
                "line_num": int(row[0]) if row[0] is not None else None,
                "item_code": row[1] or "",
                "batch_number": row[2] or "",
                "warehouse": row[3] or "",
                "quantity": Decimal(str(row[4] or 0)),
                "direction": int(row[5]),
                "is_issue": int(row[5]) == DIRECTION_OUT,
            }
            for row in rows
        ]

    def verify_allocation(
        self,
        doc_entry: int,
        expected: dict[tuple[int, str], Decimal],
        *,
        base_type: int = BASE_TYPE_STOCK_TRANSFER,
    ) -> list[str]:
        """Compare what we asked SAP to allocate against what it wrote.

        `expected` maps (line_num, batch_number) -> quantity. Returns a list of
        human-readable discrepancies; empty means the document matches.
        """
        posted: dict[tuple[int, str], Decimal] = {}
        for row in self.posted_allocations(doc_entry, base_type=base_type):
            if not row["is_issue"]:
                continue  # the issue side is the authoritative one
            key = (row["line_num"], row["batch_number"])
            posted[key] = posted.get(key, Decimal("0")) + row["quantity"]

        problems: list[str] = []
        for key, want in expected.items():
            got = posted.get(key, Decimal("0"))
            if got != Decimal(str(want)):
                line_num, batch = key
                problems.append(
                    f"line {line_num} batch {batch}: sent {want}, SAP recorded {got}"
                )
        for key in posted.keys() - expected.keys():
            line_num, batch = key
            problems.append(
                f"line {line_num} batch {batch}: SAP recorded "
                f"{posted[key]} that we did not send"
            )
        return problems

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading batches: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA batch query failed: %s", e)
            raise SAPDataError("Failed to read batch stock from SAP.") from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

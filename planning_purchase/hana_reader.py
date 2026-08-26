"""planning_purchase/hana_reader.py

Every SAP read the Planning & Purchase module makes.

**Where the plan lives.** JIVO's planners author the monthly production plan as a
SAP *sales forecast*: `OFCT` is the header, `FCT1` the lines. Verified against
the live company — all 24 headers in `JIVO_OIL_HANADB` are named
"OIL Monthly Production Planning for the <Month> <Year>", `FormView` is `M` for
monthly and `W` for weekly, and every `FCT1` line carries `Date` = the period
start with `Quantity` in the item's inventory unit.

**What unit the plan is in.** The item's `OITM.InvntryUom` — `PCS` for 97 of the
98 items on the August 2026 plan, `DRM` for the one drum SKU. `PCS` means single
bottles/tins, NOT cases. `OITM.SalFactor2` is the pieces-per-case factor
(4, 12, 16, 20, 24, 70 …) and is the *only* correct way to show the plan in the
cases the floor speaks. Production runs are entered in cases, so anything
comparing the two must convert first.

**The BOM trap.** `OITT."Qauntity"` (SAP's own typo) is the quantity the recipe
is written for, and it is frequently not 1 — on this company 159 BOMs are per-1
but 44 are per-4, 30 per-20, 29 per-16, 26 per-12. Component quantity per unit
of finished good is therefore `ITT1."Quantity" / OITT."Qauntity"`. Skipping that
division overstates every requirement by the case factor.

**Not every BOM line is a material.** `ITT1."Type"` is 4 for an inventory item and
290 for a *resource* — a conversion cost. This company has 282 resource lines and
none of them exist in `OITM`: `JWPL09240001` is "FILLING COST CANOLA AND OLIVE",
`JWPL09240005` is "PET BOTTLE BLOWING CONVERSION COST". Treating them as
components puts a 2.5-million-unit "shortage" of a cost centre at the top of the
purchase list. They are named from `ORSC`, reported separately, and never offered
for purchase. (`sap_plan_dashboard` forced the same `ItemType = 4` filter on
`WOR1` for exactly this reason.)

**Price lives on the item master, not on the last purchase order.** `POR1."Price"`
is denominated in the *purchase* unit, which is often not the inventory unit —
loose olive oil is bought by the metric ton at ~230,000 and consumed by the litre
at ~278. Costing a litre requirement at the per-ton price inflates the answer a
thousandfold. `OITM."LastPurPrc"` is per inventory unit and is the only figure
that can be multiplied by a BOM requirement. The last purchase order is still the
best evidence of *who* supplies a component, so the vendor comes from there and
the price does not.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

# SAP item groups, matched on the group name because the group is what SAP
# enforces; an item-code prefix is only a naming convention. Verified group names
# on the live company: 'PACKAGING MATERIAL' (875 items), 'RAW MATERIAL' (83).
PACKAGING_TOKENS = ("PACKAG",)
RAW_TOKENS = ("RAW", "OIL")

# Movement types on OINM. 59 = goods receipt from production (what was actually
# made); 60 = goods issue; 15 = delivery.
TRANS_TYPE_PRODUCTION_RECEIPT = 59

# ITT1."Type": 4 is an inventory item, 290 a resource (a conversion cost).
# Only a type-4 line can ever be purchased.
BOM_LINE_TYPE_ITEM = 4

# Litres in ONE piece, for a plan that an oil business reads in litres.
#
# `OITM."SalPackUn"` is where SAP records the volume of the unit a line is billed
# in, and it is the field the monthly sales-litre reports already run on. It is
# the only correct source: a 1 L bottle reads 1, a 200 ML bottle 0.2, and a
# weight-packed 869 GMS pack reads 0.9549 -- a figure no amount of parsing the
# item name could ever recover. Names state the piece volume and the carton size
# separately and lie about both, which is what an earlier name-based cascade in
# `dispatch_plans` had to be rewritten to stop doing.
#
# `U_IsLitre` is the gate, and it is load-bearing rather than decorative:
# `SalPackUn` is populated for the WHOLE item master, so on this company all 875
# packaging items carry one while none is flagged as litres. Drop the gate and a
# purchase line of 100,000 preforms reports 100,000 litres.
LITRES_PER_UNIT_SQL = """
    CASE
        WHEN UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y' THEN IFNULL(M."SalPackUn", 0)
        ELSE 0
    END
"""


def classify_material(item_group: Optional[str]) -> str:
    """PACKAGING / RAW / OTHER from the SAP item group name."""
    group = (item_group or "").upper()
    if any(token in group for token in PACKAGING_TOKENS):
        return "PACKAGING"
    if any(token in group for token in RAW_TOKENS):
        return "RAW"
    return "OTHER"


def _chunks(values: Sequence[Any], size: int = 400) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _placeholders(count: int) -> str:
    return ", ".join(["?"] * count)


class HanaProductionPlanReader:
    """Reads plans, bills of material, stock and purchasing data for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    @property
    def schema(self) -> str:
        return self.connection.schema

    # ------------------------------------------------------------------
    # The plan (OFCT / FCT1)
    # ------------------------------------------------------------------

    def list_plans(self, limit: int = 36) -> List[Dict[str, Any]]:
        """Plan headers, newest first, with line counts and planned totals.

        `LEFT JOIN` so a header a planner created but has not filled in yet still
        appears — an empty plan is a thing someone needs to see and finish, not a
        row to hide.
        """
        query = f"""
            SELECT
                H."AbsID",
                H."Code",
                H."Name",
                H."StartDate",
                H."EndDate",
                H."FormView",
                COUNT(L."ItemCode")                AS "LineCount",
                COUNT(DISTINCT L."ItemCode")       AS "ItemCount",
                IFNULL(SUM(L."Quantity"), 0)       AS "PlannedQty",
                -- Litre and case totals so the list can be read in the same unit
                -- as the detail page. Both need the item master, hence the second
                -- LEFT JOIN; it stays LEFT so an empty plan still lists.
                IFNULL(SUM(L."Quantity" * ({LITRES_PER_UNIT_SQL})), 0) AS "PlannedLitres",
                IFNULL(SUM(L."Quantity" / NULLIF(IFNULL(M."SalFactor2", 1), 0)), 0)
                                                   AS "PlannedCases",
                MIN(L."Date")                      AS "FirstBucketDate",
                MAX(L."Date")                      AS "LastBucketDate"
            FROM "{self.schema}"."OFCT" H
            LEFT JOIN "{self.schema}"."FCT1" L ON L."AbsID" = H."AbsID"
            LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
            GROUP BY H."AbsID", H."Code", H."Name", H."StartDate", H."EndDate", H."FormView"
            ORDER BY H."AbsID" DESC
            LIMIT {int(limit)}
        """
        return self._rows(query)

    def get_plan_header(self, abs_id: int) -> Optional[Dict[str, Any]]:
        query = f"""
            SELECT "AbsID", "Code", "Name", "StartDate", "EndDate", "FormView", "UserSign"
            FROM "{self.schema}"."OFCT"
            WHERE "AbsID" = ?
        """
        rows = self._rows(query, [int(abs_id)])
        return rows[0] if rows else None

    def get_plan_lines(self, abs_id: int) -> List[Dict[str, Any]]:
        """Plan lines enriched with the item master needed to read them.

        `SalFactor2` is the pieces-per-case factor and is what turns a plan in
        PCS into the cases production actually reports. `TreeType = 'P'` on the
        item master is the flag for "this has a production BOM"; the LEFT JOIN to
        OITT confirms one really exists, because the two do disagree — four items
        on the August 2026 plan claim no BOM and have none.
        """
        query = f"""
            SELECT
                L."LineID",
                L."ItemCode",
                M."ItemName",
                L."Date"                            AS "BucketDate",
                L."Quantity"                        AS "PlannedQty",
                IFNULL(L."WhsCode", '')             AS "WhsCode",
                IFNULL(M."InvntryUom", '')          AS "Uom",
                IFNULL(M."SalFactor2", 1)           AS "PiecesPerCase",
                {LITRES_PER_UNIT_SQL}               AS "LitresPerUnit",
                IFNULL(G."ItmsGrpNam", '')          AS "ItemGroup",
                IFNULL(M."TreeType", 'N')           AS "TreeType",
                CASE WHEN T."Code" IS NULL THEN 0 ELSE 1 END AS "HasBom",
                IFNULL(T."Qauntity", 0)             AS "BomBaseQty"
            FROM "{self.schema}"."FCT1" L
            LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
            LEFT JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
            LEFT JOIN "{self.schema}"."OITT" T
                   ON T."Code" = L."ItemCode" AND T."TreeType" = 'P'
            WHERE L."AbsID" = ?
            ORDER BY L."Date", L."ItemCode"
        """
        return self._rows(query, [int(abs_id)])

    # ------------------------------------------------------------------
    # Bills of material (OITT / ITT1)
    # ------------------------------------------------------------------

    def get_bom_components(self, item_codes: Sequence[str]) -> List[Dict[str, Any]]:
        """Single-level production BOM for each parent, per ONE unit of parent.

        `QtyPerUnit` is computed in SQL as `ITT1."Quantity" / OITT."Qauntity"` so
        no caller can forget the division. A base quantity of zero would be
        corrupt master data, and `NULLIF` turns it into NULL rather than a
        division error — the row then surfaces as an unusable BOM instead of
        taking the request down.

        Both material and resource lines come back, tagged by `LineType`
        (4 = inventory item, 290 = resource/conversion cost) with the resource
        name resolved from `ORSC`. The caller splits them: a conversion cost
        belongs on the plan's cost picture, never on its purchase list.

        `HasOwnBom` marks a component that is itself manufactured (79 such items
        on this company). Those are reported, never auto-exploded: whether a
        preform is blown in-house or bought is a business decision, not something
        a requirement query should assume.
        """
        if not item_codes:
            return []

        out: List[Dict[str, Any]] = []
        for chunk in _chunks(list(item_codes)):
            query = f"""
                SELECT
                    T."Code"                              AS "ParentCode",
                    T."Qauntity"                          AS "BomBaseQty",
                    C."ChildNum",
                    C."Code"                              AS "ComponentCode",
                    C."Type"                              AS "LineType",
                    COALESCE(M."ItemName", R."ResName", '') AS "ComponentName",
                    C."Quantity"                          AS "BomQty",
                    C."Quantity" / NULLIF(T."Qauntity", 0) AS "QtyPerUnit",
                    IFNULL(C."Warehouse", '')             AS "IssueWarehouse",
                    IFNULL(M."InvntryUom", '')            AS "Uom",
                    IFNULL(G."ItmsGrpNam", '')            AS "ItemGroup",
                    IFNULL(M."PrchseItem", 'N')           AS "PurchaseItem",
                    IFNULL(M."LastPurPrc", 0)             AS "LastPurchasePrice",
                    CASE WHEN SUB."Code" IS NULL THEN 0 ELSE 1 END AS "HasOwnBom"
                FROM "{self.schema}"."OITT" T
                JOIN "{self.schema}"."ITT1" C ON C."Father" = T."Code"
                LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = C."Code"
                LEFT JOIN "{self.schema}"."ORSC" R ON R."ResCode" = C."Code"
                LEFT JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
                LEFT JOIN "{self.schema}"."OITT" SUB
                       ON SUB."Code" = C."Code" AND SUB."TreeType" = 'P'
                WHERE T."TreeType" = 'P'
                  AND T."Code" IN ({_placeholders(len(chunk))})
                ORDER BY T."Code", C."ChildNum"
            """
            out.extend(self._rows(query, list(chunk)))
        return out

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def get_item_stock(
        self, item_codes: Sequence[str], warehouses: Optional[Sequence[str]] = None
    ) -> List[Dict[str, Any]]:
        """Per-warehouse on-hand and benchmark for each component.

        Read from the same `OITW` join the Stock Benchmark dashboard uses, so a
        number here and a number there cannot disagree.

        One deliberate difference: the Stock Benchmark reports an item unconsumed
        for 30+ days as "none" and drops it from its low/critical counts. That
        rule is wrong for planned requirement — a component the new plan needs in
        quantity is exactly the one to flag whether or not it moved last month —
        so movement age is returned as data and never used to suppress a row.
        """
        if not item_codes:
            return []

        out: List[Dict[str, Any]] = []
        for chunk in _chunks(list(item_codes)):
            params: List[Any] = list(chunk)
            wh_clause = ""
            if warehouses:
                wh_clause = f'AND W."WhsCode" IN ({_placeholders(len(warehouses))})'
                params.extend(warehouses)

            query = f"""
                SELECT
                    W."ItemCode",
                    W."WhsCode",
                    W."OnHand",
                    W."MinStock",
                    W."IsCommited"                  AS "Committed",
                    W."OnOrder",
                    IFNULL(M."InvntryUom", '')      AS "Uom",
                    IFNULL(M."LastPurPrc", 0)       AS "LastPurchasePrice",
                    IFNULL(G."ItmsGrpNam", '')      AS "ItemGroup",
                    mov."LastConsumptionDate",
                    CASE
                        WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                        ELSE DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)
                    END                             AS "DaysSinceLastConsumption"
                FROM "{self.schema}"."OITW" W
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = W."ItemCode"
                LEFT JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
                LEFT JOIN (
                    SELECT N."ItemCode", MAX(N."DocDate") AS "LastConsumptionDate"
                    FROM "{self.schema}"."OINM" N
                    WHERE N."OutQty" > 0 AND N."TransType" IN (15, 60, 202)
                    GROUP BY N."ItemCode"
                ) mov ON mov."ItemCode" = W."ItemCode"
                WHERE W."ItemCode" IN ({_placeholders(len(chunk))})
                {wh_clause}
                ORDER BY W."ItemCode", W."WhsCode"
            """
            out.extend(self._rows(query, params))
        return out

    def get_commitment_breakdown(
        self, item_code: str, warehouse: str
    ) -> List[Dict[str, Any]]:
        """The documents that make up `OITW."IsCommited"` for one item + warehouse.

        SAP publishes committed stock as a single number with no explanation, and
        it is the figure that decides whether a component reads as available. On
        this company three document types reserve stock, and together they
        reconcile to the penny:

            RM0000003 @ BH-LO   20,666 = 20,666 production  +      0 transfer
            PM0000235 @ BH-PM  179,000 =      0 production  + 179,000 transfer
            PM0000121 @ BH-PC   47,429 = 47,400 production  +     29 transfer

        A production order reserves `PlannedQty - IssuedQty` of each component
        while it is Planned or Released — what it still has to draw. A transfer
        request reserves its open quantity at the *sending* warehouse. A sales
        order reserves its open quantity, which for a factory is rare but real.

        The caller compares the total against `IsCommited` and says so when they
        disagree, rather than presenting a partial list as the whole story.
        """
        query = f"""
            SELECT
                'PRODUCTION_ORDER'              AS "Source",
                W."DocEntry",
                W."DocNum",
                W."Status"                      AS "DocStatus",
                W."ItemCode"                    AS "RefCode",
                IFNULL(M."ItemName", '')        AS "RefName",
                C."PlannedQty",
                C."IssuedQty",
                (C."PlannedQty" - C."IssuedQty") AS "CommittedQty",
                W."DueDate"                     AS "DueDate",
                W."PostDate"                    AS "DocDate",
                ''                              AS "ToWarehouse"
            FROM "{self.schema}"."OWOR" W
            JOIN "{self.schema}"."WOR1" C ON C."DocEntry" = W."DocEntry"
            LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = W."ItemCode"
            WHERE C."ItemCode" = ?
              AND C."wareHouse" = ?
              AND W."Status" IN ('P', 'R')
              AND (C."PlannedQty" - C."IssuedQty") <> 0

            UNION ALL

            SELECT
                'TRANSFER_REQUEST'              AS "Source",
                H."DocEntry",
                H."DocNum",
                H."DocStatus",
                IFNULL(L."WhsCode", '')         AS "RefCode",
                ''                              AS "RefName",
                L."Quantity"                    AS "PlannedQty",
                (L."Quantity" - L."OpenQty")    AS "IssuedQty",
                L."OpenQty"                     AS "CommittedQty",
                H."DocDueDate"                  AS "DueDate",
                H."DocDate"                     AS "DocDate",
                IFNULL(L."WhsCode", '')         AS "ToWarehouse"
            FROM "{self.schema}"."OWTQ" H
            JOIN "{self.schema}"."WTQ1" L ON L."DocEntry" = H."DocEntry"
            WHERE L."ItemCode" = ?
              AND L."FromWhsCod" = ?
              AND H."DocStatus" = 'O'
              AND L."LineStatus" = 'O'
              AND L."OpenQty" <> 0

            UNION ALL

            SELECT
                'SALES_ORDER'                   AS "Source",
                H."DocEntry",
                H."DocNum",
                H."DocStatus",
                IFNULL(H."CardCode", '')        AS "RefCode",
                IFNULL(H."CardName", '')        AS "RefName",
                L."Quantity"                    AS "PlannedQty",
                (L."Quantity" - L."OpenQty")    AS "IssuedQty",
                L."OpenQty"                     AS "CommittedQty",
                L."ShipDate"                    AS "DueDate",
                H."DocDate"                     AS "DocDate",
                ''                              AS "ToWarehouse"
            FROM "{self.schema}"."ORDR" H
            JOIN "{self.schema}"."RDR1" L ON L."DocEntry" = H."DocEntry"
            WHERE L."ItemCode" = ?
              AND L."WhsCode" = ?
              AND H."DocStatus" = 'O'
              AND L."LineStatus" = 'O'
              AND L."OpenQty" <> 0

            ORDER BY "DueDate"
        """
        params = [item_code, warehouse] * 3
        return self._rows(query, params)

    def get_item_warehouse_stock(
        self, item_code: str, warehouse: str
    ) -> Optional[Dict[str, Any]]:
        """The single `OITW` row a commitment breakdown has to reconcile against."""
        rows = self._rows(
            f"""
            SELECT W."ItemCode", W."WhsCode", W."OnHand", W."IsCommited", W."OnOrder",
                   IFNULL(M."ItemName", '') AS "ItemName",
                   IFNULL(M."InvntryUom", '') AS "Uom"
            FROM "{self.schema}"."OITW" W
            LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = W."ItemCode"
            WHERE W."ItemCode" = ? AND W."WhsCode" = ?
            """,
            [item_code, warehouse],
        )
        return rows[0] if rows else None

    def get_open_purchase_qty(self, item_codes: Sequence[str]) -> List[Dict[str, Any]]:
        """Quantity already on open purchase orders, per item, with the nearest due date.

        Netting this off is not optional. Without it the same shortage is raised
        every cycle until the goods arrive, which is the fastest way to make an
        alarm list untrustworthy. 762 lines were open on this company when the
        module was built, so it is a real number, not an edge case.
        """
        if not item_codes:
            return []

        out: List[Dict[str, Any]] = []
        for chunk in _chunks(list(item_codes)):
            query = f"""
                SELECT
                    L."ItemCode",
                    SUM(L."OpenQty")   AS "OpenQty",
                    MIN(L."ShipDate")  AS "EarliestDue",
                    MAX(L."ShipDate")  AS "LatestDue",
                    COUNT(*)           AS "OpenLines"
                FROM "{self.schema}"."OPOR" H
                JOIN "{self.schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
                WHERE H."DocStatus" = 'O'
                  AND L."LineStatus" = 'O'
                  AND L."ItemCode" IN ({_placeholders(len(chunk))})
                GROUP BY L."ItemCode"
            """
            out.extend(self._rows(query, list(chunk)))
        return out

    def get_last_vendors(self, item_codes: Sequence[str]) -> List[Dict[str, Any]]:
        """The supplier each component was last bought from.

        The item master is not usable for this — `OITM."CardCode"` is empty for
        all 2,026 purchase items on this company — so the last purchase order is
        the only evidence of who actually supplies a component.

        `Price` comes back too, but as **reference only**: it is denominated in
        the purchase unit, which for bulk oils is a metric ton against a litre
        BOM. Costing is done from `OITM."LastPurPrc"`. See the module docstring.
        """
        if not item_codes:
            return []

        out: List[Dict[str, Any]] = []
        for chunk in _chunks(list(item_codes)):
            query = f"""
                SELECT "ItemCode", "CardCode", "CardName", "Price", "Currency", "DocDate"
                FROM (
                    SELECT
                        L."ItemCode",
                        H."CardCode",
                        H."CardName",
                        L."Price",
                        L."Currency",
                        H."DocDate",
                        ROW_NUMBER() OVER (
                            PARTITION BY L."ItemCode" ORDER BY H."DocDate" DESC, H."DocEntry" DESC
                        ) AS rn
                    FROM "{self.schema}"."OPOR" H
                    JOIN "{self.schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
                    WHERE L."ItemCode" IN ({_placeholders(len(chunk))})
                )
                WHERE rn = 1
            """
            out.extend(self._rows(query, list(chunk)))
        return out

    # ------------------------------------------------------------------
    # Actuals, for plan vs production
    # ------------------------------------------------------------------

    def get_produced_quantities(
        self, item_codes: Sequence[str], date_from, date_to
    ) -> List[Dict[str, Any]]:
        """What was actually produced, per item, from SAP's own movement journal.

        `OINM` inward quantity with `TransType = 59` is the goods receipt from
        production: the figure SAP and finance agree on, stamped with the posting
        date. Returned in the item's inventory unit, the same unit the plan is
        in, so plan and actual are directly comparable without conversion.
        """
        if not item_codes:
            return []

        out: List[Dict[str, Any]] = []
        for chunk in _chunks(list(item_codes)):
            params: List[Any] = [date_from, date_to] + list(chunk)
            query = f"""
                SELECT
                    N."ItemCode",
                    SUM(N."InQty")     AS "ProducedQty",
                    COUNT(*)           AS "Receipts",
                    MIN(N."DocDate")   AS "FirstReceipt",
                    MAX(N."DocDate")   AS "LastReceipt"
                FROM "{self.schema}"."OINM" N
                WHERE N."TransType" = {TRANS_TYPE_PRODUCTION_RECEIPT}
                  AND N."InQty" > 0
                  AND N."DocDate" >= ?
                  AND N."DocDate" <= ?
                  AND N."ItemCode" IN ({_placeholders(len(chunk))})
                GROUP BY N."ItemCode"
            """
            out.extend(self._rows(query, params))
        return out

    def get_daily_produced_quantities(
        self, item_codes: Sequence[str], date_from, date_to
    ) -> List[Dict[str, Any]]:
        """Same as above, broken out by posting date so actuals can be bucketed."""
        if not item_codes:
            return []

        out: List[Dict[str, Any]] = []
        for chunk in _chunks(list(item_codes)):
            params: List[Any] = [date_from, date_to] + list(chunk)
            query = f"""
                SELECT N."ItemCode", N."DocDate", SUM(N."InQty") AS "ProducedQty"
                FROM "{self.schema}"."OINM" N
                WHERE N."TransType" = {TRANS_TYPE_PRODUCTION_RECEIPT}
                  AND N."InQty" > 0
                  AND N."DocDate" >= ?
                  AND N."DocDate" <= ?
                  AND N."ItemCode" IN ({_placeholders(len(chunk))})
                GROUP BY N."ItemCode", N."DocDate"
                ORDER BY N."DocDate"
            """
            out.extend(self._rows(query, params))
        return out

    # ------------------------------------------------------------------
    # Dropdowns
    # ------------------------------------------------------------------

    def get_vendors(self, search: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = "WHERE \"CardType\" = 'S' AND \"frozenFor\" = 'N'"
        if search:
            where += ' AND (UPPER("CardName") LIKE ? OR UPPER("CardCode") LIKE ?)'
            token = f"%{search.upper()}%"
            params.extend([token, token])

        query = f"""
            SELECT "CardCode", "CardName", IFNULL("Currency", '') AS "Currency"
            FROM "{self.schema}"."OCRD"
            {where}
            ORDER BY "CardName"
            LIMIT {int(limit)}
        """
        return self._rows(query, params)

    def get_warehouses(self) -> List[Dict[str, Any]]:
        query = f"""
            SELECT "WhsCode", IFNULL("WhsName", '') AS "WhsName"
            FROM "{self.schema}"."OWHS"
            WHERE IFNULL("Inactive", 'N') = 'N'
            ORDER BY "WhsCode"
        """
        return self._rows(query)

    def get_branch_for_warehouse(self, warehouse_code: str) -> Optional[int]:
        """`BPLid` for a warehouse, needed on the SAP payload in a multi-branch company."""
        if not warehouse_code:
            return None
        rows = self._rows(
            f'SELECT "BPLid" FROM "{self.schema}"."OWHS" WHERE "WhsCode" = ?',
            [warehouse_code],
        )
        return rows[0]["BPLid"] if rows and rows[0].get("BPLid") is not None else None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _rows(self, query: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        """Run one statement and return dicts. Connection is always closed."""
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed (planning_purchase): %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA query error (planning_purchase): %s", e)
            raise SAPDataError(
                "Failed to read planning data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error("SAP HANA data error (planning_purchase): %s", e)
            raise SAPDataError(
                "Failed to read planning data from SAP. Please try again later."
            ) from e
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

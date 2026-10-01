import logging
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# SAP item group (OITB."ItmsGrpNam") that holds the finished goods a line
# produces. Verified 2026-09-10 against all three schemas — Oil, Beverages and
# Mart each name it exactly this, and every completed production run's item
# sits in it; the strays (RM/SL codes) are all abandoned drafts.
FINISHED_GOODS_ITEM_GROUP = 'FINISHED'

# A bill of material carries two kinds of line, and only one of them is stuff.
# `ITT1."Type"` / `WOR1."ItemType"` is 4 for an inventory item and 290 for a
# **resource** — a conversion cost such as `JWPL09240002 Filling Cost
# Commodities`, which lives in `ORSC`, not `OITM`. A resource has no item
# master, so it has no warehouse stock, no UoM and no purchase price, and the
# store can never hand one over: a BOM request that carries one is a request
# nobody can approve, because its in-stock reads 0 and always will. Every
# reader here therefore returns materials only. Planning & Purchase forced the
# same filter for the same reason, and the plan check separates resource lines
# out rather than dropping them so the screen can still show the whole recipe.
BOM_LINE_TYPE_ITEM = 4


class SAPReadError(Exception):
    pass


class ProductionOrderReader:
    """Reads production orders from SAP HANA for a specific company.

    When HANA cannot be reached, what starting a run needs -- the run-startable
    item search, an item's BOM, its pieces per case and litres per piece -- is
    answered from the nightly copy (``sap_mirror``, list ``boms``). Production
    orders are not copied, so a run started from one still needs SAP.
    ``use_copy=False`` is for the job that takes the copy.
    """

    def __init__(self, company_code: str, *, use_copy: bool = True):
        self.company_code = company_code
        self.use_copy = use_copy
        try:
            self.client = SAPClient(company_code=company_code)
        except Exception as e:
            raise SAPReadError(f"Failed to initialize SAP client: {e}")

    def _from_copy(self, exc, serve):
        """Answer a failed read from the BOM copy, or re-raise ``exc``.

        ``serve(items)`` gets the copy's rows -- one per run-startable item --
        and returns the answer, or ``None`` when the copy cannot give one.
        """
        from sap_mirror import services as sap_mirror

        if not self.use_copy or not sap_mirror.hana_unreachable(exc):
            raise exc
        try:
            copy = sap_mirror.copied_rows(self.company_code, sap_mirror.PRODUCTION_BOMS)
            result = None if copy is None else serve(copy[0])
        except Exception:  # noqa: BLE001 -- a broken copy must not change what the caller sees
            logger.exception("SAP BOM copy could not answer for %s", self.company_code)
            raise exc
        if result is None:
            raise exc
        logger.warning("HANA unreachable; %s answered from the copy of %s", self.company_code, copy[1])
        return result

    def get_released_production_orders(self) -> list:
        """Get all released production orders with remaining qty > 0."""
        sql = """
            SELECT
                W."DocEntry",
                W."DocNum",
                W."ItemCode",
                W."ProdName",
                W."PlannedQty",
                W."CmpltQty",
                W."RjctQty",
                (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                W."StartDate",
                W."DueDate",
                W."Warehouse",
                W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."Status" = 'R'
              AND (W."PlannedQty" - W."CmpltQty" - W."RjctQty") > 0
            ORDER BY W."DueDate" ASC
        """.format(schema=self.client.context.config['hana']['schema'])
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch released production orders: {e}")
            raise SAPReadError(f"Failed to fetch production orders: {e}")

    def get_open_production_orders(self) -> list:
        """Get planned/released production orders with remaining qty > 0."""
        sql = """
            SELECT
                W."DocEntry",
                W."DocNum",
                W."ItemCode",
                W."ProdName",
                W."PlannedQty",
                W."CmpltQty",
                W."RjctQty",
                (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                W."StartDate",
                W."DueDate",
                W."Warehouse",
                W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."Status" IN ('P', 'R')
              AND (W."PlannedQty" - W."CmpltQty" - W."RjctQty") > 0
            ORDER BY W."DueDate" ASC
        """.format(schema=self.client.context.config['hana']['schema'])
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch open production orders: {e}")
            raise SAPReadError(f"Failed to fetch production orders: {e}")

    def get_production_order_detail(self, doc_entry: int) -> dict:
        """Get full detail of a production order including components.
        Tries DocEntry first, falls back to DocNum if not found."""
        schema = self.client.context.config['hana']['schema']
        safe_val = int(doc_entry)

        header_sql = """
            SELECT
                W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                W."PlannedQty", W."CmpltQty", W."RjctQty",
                (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                W."StartDate", W."DueDate", W."Warehouse", W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."DocEntry" = {val}
        """.format(schema=schema, val=safe_val)

        headers = self._execute(header_sql)

        # Fallback: value might be DocNum instead of DocEntry
        if not headers:
            header_sql = """
                SELECT
                    W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                    W."PlannedQty", W."CmpltQty", W."RjctQty",
                    (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                    W."StartDate", W."DueDate", W."Warehouse", W."Status"
                FROM "{schema}"."OWOR" W
                WHERE W."DocNum" = {val}
            """.format(schema=schema, val=safe_val)
            headers = self._execute(header_sql)

        if not headers:
            raise SAPReadError(f"Production order {doc_entry} not found.")

        actual_doc_entry = headers[0]['DocEntry']
        components_sql = """
            SELECT
                C."LineNum", C."ItemCode", C."ItemName", C."PlannedQty",
                C."IssuedQty", C."wareHouse" AS "Warehouse", C."UomCode",
                I."LastPurPrc" AS "UnitPrice"
            FROM "{schema}"."WOR1" C
            LEFT JOIN "{schema}"."OITM" I ON C."ItemCode" = I."ItemCode"
            WHERE C."DocEntry" = {val}
              AND C."ItemType" = {item_line}
            ORDER BY C."LineNum" ASC
        """.format(
            schema=schema,
            val=int(actual_doc_entry),
            item_line=BOM_LINE_TYPE_ITEM,
        )

        components = self._execute(components_sql)
        return {
            'header': headers[0],
            'components': components,
        }

    def get_production_orders_by_entries(self, doc_entries: list) -> dict:
        """Batch fetch production orders by doc entries.
        Returns {lookup_value: row_dict} — tries DocEntry first, falls back to DocNum."""
        if not doc_entries:
            return {}
        schema = self.client.context.config['hana']['schema']
        entries_str = ', '.join(str(int(e)) for e in doc_entries)
        sql = """
            SELECT
                W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                W."PlannedQty", W."CmpltQty", W."RjctQty",
                W."StartDate", W."DueDate", W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."DocEntry" IN ({entries})
        """.format(schema=schema, entries=entries_str)
        try:
            rows = self._execute(sql)
            result = {r['DocEntry']: r for r in rows}

            # For any values not found by DocEntry, try DocNum fallback
            missing = [e for e in doc_entries if e not in result]
            if missing:
                missing_str = ', '.join(str(int(e)) for e in missing)
                fallback_sql = """
                    SELECT
                        W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                        W."PlannedQty", W."CmpltQty", W."RjctQty",
                        W."StartDate", W."DueDate", W."Status"
                    FROM "{schema}"."OWOR" W
                    WHERE W."DocNum" IN ({entries})
                """.format(schema=schema, entries=missing_str)
                fallback_rows = self._execute(fallback_sql)
                for r in fallback_rows:
                    # Key by DocNum so the caller can look up by the value it passed
                    result[r['DocNum']] = r

            return result
        except Exception as e:
            logger.error(f"Failed to batch fetch production orders: {e}")
            raise SAPReadError(f"Failed to batch fetch production orders: {e}")

    def resolve_item_code_by_name(self, name: str) -> str:
        """Resolve a SAP item code (OITM.ItemCode) from an exact ItemName.

        Returns the code only when the name maps to exactly one item; returns
        None when there is no match or the name is ambiguous (so callers can
        surface a clear error instead of silently picking the wrong item).
        """
        if not name:
            return None
        schema = self.client.context.config['hana']['schema']
        safe_name = name.replace("'", "''")
        try:
            # The value may already be a valid item code (some older runs stored
            # the code in the product field) — use it directly if so.
            code_rows = self._execute(
                'SELECT "ItemCode" FROM "{schema}"."OITM" WHERE "ItemCode" = \'{v}\''
                .format(schema=schema, v=safe_name)
            )
            if len(code_rows) == 1:
                return code_rows[0]['ItemCode']
            # Otherwise resolve from the item name (unique match only).
            rows = self._execute(
                'SELECT "ItemCode" FROM "{schema}"."OITM" WHERE "ItemName" = \'{v}\''
                .format(schema=schema, v=safe_name)
            )
        except Exception as e:
            logger.error(f"Failed to resolve item code for '{name}': {e}")
            raise SAPReadError(f"Failed to resolve item code for '{name}': {e}")
        if len(rows) == 1:
            return rows[0]['ItemCode']
        if len(rows) > 1:
            logger.warning(
                f"Item name '{name}' is ambiguous — matches {len(rows)} SAP items; "
                f"cannot resolve a single item code."
            )
        return None

    def get_pieces_per_case_map(self, item_codes: list) -> dict:
        """Batch bottles-per-case lookup from OITM.SalFactor2.

        SalFactor2 is the transacted pieces-per-box/case everywhere else in the
        system (box generation, dispatch scans), so it is also the conversion
        between case counts and per-bottle machine speeds. Returns
        ``{item_code: int}`` with unconfigured items (SalFactor2 null/0) absent.
        """
        codes = sorted({str(c).strip() for c in item_codes if c and str(c).strip()})
        if not codes:
            return {}
        schema = self.client.context.config['hana']['schema']
        in_list = ', '.join("'{}'".format(c.replace("'", "''")) for c in codes)
        sql = """
            SELECT "ItemCode", "SalFactor2"
            FROM "{schema}"."OITM"
            WHERE "ItemCode" IN ({in_list})
        """.format(schema=schema, in_list=in_list)
        try:
            rows = self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch SalFactor2 for {len(codes)} items: {e}")
            return self._from_copy(
                _caused_by(SAPReadError(f"Failed to fetch pieces-per-case: {e}"), e),
                lambda items: _copied_map(items, codes, "pieces_per_case"),
            )
        result = {}
        for row in rows:
            try:
                factor = int(row.get('SalFactor2') or 0)
            except (TypeError, ValueError):
                factor = 0
            if factor > 0:
                result[row['ItemCode']] = factor
        return result

    def get_litres_per_piece_map(self, item_codes: list) -> dict:
        """Batch litres-per-piece lookup from OITM.SalPackUn.

        SalPackUn holds the volume of one billed piece — a 5 LTR tin reads 5, a
        250 ML bottle 0.25, a "1 LTR + 1 LTR" combo 2 — and it is the single
        source the whole app takes litres from (see
        ``dispatch_plans.hana_reader._litres_per_unit_expr``). Never parse the
        SKU name for volume: names state the piece volume and the carton size
        separately and lie about both.

        ``U_IsLitre`` is the gate — cartons and preforms carry a SalPackUn too.
        Returns ``{item_code: float}``; items that hold no liquid are absent, so
        a caller can tell "no volume" from "zero litres".
        """
        codes = sorted({str(c).strip() for c in item_codes if c and str(c).strip()})
        if not codes:
            return {}
        schema = self.client.context.config['hana']['schema']
        in_list = ', '.join("'{}'".format(c.replace("'", "''")) for c in codes)
        sql = """
            SELECT "ItemCode", "SalPackUn"
            FROM "{schema}"."OITM"
            WHERE "ItemCode" IN ({in_list})
              AND UPPER(IFNULL("U_IsLitre", 'N')) = 'Y'
        """.format(schema=schema, in_list=in_list)
        try:
            rows = self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch SalPackUn for {len(codes)} items: {e}")
            return self._from_copy(
                _caused_by(SAPReadError(f"Failed to fetch litres-per-piece: {e}"), e),
                lambda items: _copied_map(items, codes, "litres_per_piece"),
            )
        result = {}
        for row in rows:
            try:
                litres = float(row.get('SalPackUn') or 0)
            except (TypeError, ValueError):
                litres = 0.0
            if litres > 0:
                result[row['ItemCode']] = litres
        return result

    def get_resource_codes(self, codes: list) -> set:
        """Which of these codes are SAP **resources** (`ORSC`) rather than items.

        Asked of codes an item-master lookup did not find, so that a caller can
        tell the two reasons for that apart: a resource, which is a conversion
        cost and never material, from an item that has merely gone missing —
        which still belongs on a request, because a line nobody is asked for is
        a line nobody picks.
        """
        wanted = [c for c in (codes or []) if c]
        if not wanted:
            return set()

        schema = self.client.context.config['hana']['schema']
        safe = ', '.join("'" + str(c).replace("'", "''") + "'" for c in wanted)
        sql = """
            SELECT R."ResCode" FROM "{schema}"."ORSC" R
            WHERE R."ResCode" IN ({codes})
        """.format(schema=schema, codes=safe)
        return {row['ResCode'] for row in self._execute(sql)}

    def get_material_types(self, item_codes: list) -> dict:
        """Classify items as RAW / PACKAGING / OTHER from their SAP item group.

        The same `OITB."ItmsGrpNam"` classification Planning & Purchase uses, so
        a component that reads as packing material on one screen cannot read as
        raw material on another. Items the query does not find are simply
        absent; the caller decides what an unclassifiable component means.
        """
        from planning_purchase.hana_reader import classify_material

        codes = [c for c in (item_codes or []) if c]
        if not codes:
            return {}

        schema = self.client.context.config['hana']['schema']
        safe = ', '.join("'" + str(c).replace("'", "''") + "'" for c in codes)
        sql = """
            SELECT M."ItemCode", IFNULL(G."ItmsGrpNam", '') AS "ItemGroup"
            FROM "{schema}"."OITM" M
            LEFT JOIN "{schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
            WHERE M."ItemCode" IN ({codes})
        """.format(schema=schema, codes=safe)
        rows = self._execute(sql)
        return {
            row['ItemCode']: classify_material(row.get('ItemGroup'))
            for row in rows
        }

    def get_bom_by_item_code(self, item_code: str) -> list:
        """Fetch the **material** components of a finished good's BOM (OITT/ITT1).

        `PlannedQty` comes back as the quantity for ONE BOX, which is the unit
        production is planned and entered in. Getting there needs both halves of
        SAP's own arithmetic:

        `ITT1."Quantity"` is the quantity for one *batch* of the recipe, and the
        batch size is `OITT."Qauntity"` — the tree yield, counted in the parent's
        inventory UoM, which on this data is a single bottle (`INV1` sells
        `FG0000323` at Rs 6.27 a PCS; the "(12 PCS)" in the name is carton
        config). Dividing gives the per-bottle rate, and `OITM."SalFactor2"`
        turns that into a per-box one.

        Skipping the division is only invisible while the yield happens to equal
        the case size, which is how it survived: 274 of Beverages' 296 finished
        goods are written that way, and every oil SKU is
        (`FG0000121 CANOLA OIL 1 LTR 20 PCS` — yield 20, 20 bottles, 20 caps and
        *one* carton). The 22 that are not were silently wrong by the case
        factor. `FG0000328 PET BOTTLE 250 ML ... (24 PCS)` has yield 1, so its
        lines are authored per bottle — one preform, one cap, 0.14 g of label —
        and read as per-box they asked for one preform per 24-bottle case.

        The multiplication comes before the division on purpose. A carton that
        is 1 per 3-pack divides to 0.333… first, and 0.333… x 3 is 0.999…9 — a
        whole carton turned into a fraction of one by arithmetic alone.

        A yield of zero is corrupt master data; `NULLIF` makes it a NULL
        `PlannedQty` rather than a division error, so the line surfaces as
        unusable instead of taking the whole BOM down. A missing `SalFactor2`
        falls back to the yield, which reproduces the per-batch figure — the
        right answer whenever the recipe is written a box at a time, and the
        only honest one when nothing says how big a box is.

        Resource lines are left out: see :data:`BOM_LINE_TYPE_ITEM`. They are not
        material, nobody issues them, and carried into a run they become a
        warehouse request for something the store does not have and cannot get.
        """
        safe_item = item_code.replace("'", "''")
        sql = self._bom_sql(f'T0."Code" = \'{safe_item}\'')
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch BOM for item {item_code}: {e}")
            return self._from_copy(
                _caused_by(SAPReadError(f"Failed to fetch BOM for item {item_code}: {e}"), e),
                lambda items: _copied_bom(items, item_code),
            )

    def get_all_boms_for_copy(self) -> list:
        """Every run-startable item with its BOM, pieces per case and litres per
        piece -- one row per item, as the nightly copy keeps it. Three round trips
        for the whole company, not one per item."""
        items = self.search_items(limit=None, produced_only=True)
        codes = [item["ItemCode"] for item in items]
        boms = {code: [] for code in codes}
        if codes:
            father = self._produced_items_sql()
            for line in self._execute(self._bom_sql(f'T0."Code" IN ({father})', with_father=True)):
                boms.setdefault(line.pop("Father"), []).append(line)
        pieces = self.get_pieces_per_case_map(codes)
        litres = self.get_litres_per_piece_map(codes)
        return [
            {
                "item": item,
                "bom": boms.get(item["ItemCode"], []),
                "pieces_per_case": pieces.get(item["ItemCode"]),
                "litres_per_piece": litres.get(item["ItemCode"]),
            }
            for item in items
        ]

    def _bom_sql(self, where: str, *, with_father: bool = False) -> str:
        schema = self.client.context.config['hana']['schema']
        father = ',\n                T0."Code"      AS "Father"' if with_father else ''
        return """
            SELECT
                T1."Code"      AS "ItemCode",
                T1."ItemName"  AS "ItemName",
                T1."Quantity" * COALESCE(NULLIF(F."SalFactor2", 0), T0."Qauntity")
                    / NULLIF(T0."Qauntity", 0)
                               AS "PlannedQty",
                T1."Quantity"  AS "BomQty",
                T0."Qauntity"  AS "BomBaseQty",
                COALESCE(NULLIF(F."SalFactor2", 0), T0."Qauntity") AS "PiecesPerCase",
                COALESCE(T1."Uom", I."InvntryUom") AS "UomCode",
                T1."Warehouse" AS "Warehouse",
                I."LastPurPrc" AS "UnitPrice"{father}
            FROM "{schema}"."OITT" T0
            INNER JOIN "{schema}"."ITT1" T1 ON T0."Code" = T1."Father"
            LEFT JOIN "{schema}"."OITM" I ON T1."Code" = I."ItemCode"
            LEFT JOIN "{schema}"."OITM" F ON T0."Code" = F."ItemCode"
            WHERE {where}
              AND T1."Type" = {item_line}
            ORDER BY T0."Code" ASC, T1."VisOrder" ASC
        """.format(schema=schema, where=where, father=father, item_line=BOM_LINE_TYPE_ITEM)

    def get_bom_components_for_run(self, sap_doc_entry: int = None, item_code: str = None) -> list:
        """
        Fetch BOM components with priority:
        1. If sap_doc_entry provided → fetch from WOR1 (production order components)
        2. Else if item_code provided → fetch from OITT/ITT1 (item BOM master)
        Returns a normalized list of dicts with keys:
            ItemCode, ItemName, PlannedQty, IssuedQty, UomCode
        """
        if sap_doc_entry:
            detail = self.get_production_order_detail(sap_doc_entry)
            return detail.get('components', [])
        elif item_code:
            components = self.get_bom_by_item_code(item_code)
            # Normalize: BOM master doesn't have IssuedQty
            for comp in components:
                comp.setdefault('IssuedQty', 0)
            return components
        return []

    def search_items(self, search: str = '', limit: int = 50, produced_only: bool = False) -> list:
        """Search SAP item master (OITM).

        When produced_only=True, restrict to the finished goods a production run
        can be started for: an item in the FINISHED item group that also has a
        production BOM. Otherwise return all items (e.g. raw-material lookup).

        Both halves are needed. Membership of OITT alone is not "a finished
        good" — a handful of raw materials, packing materials and sales kits
        carry recipes too (cold-pressed loose oil is genuinely produced), and
        offering those on the FG picker is how a run ends up planned against
        RM0000002. The item group alone is not enough either: without a BOM
        there are no material lines to scale, so the readiness check has nothing
        to price.
        """
        schema = self.client.context.config['hana']['schema']
        where_clause = 'WHERE 1=1'
        if search:
            safe_search = search.replace("'", "''")
            where_clause += (
                f" AND (LOWER(T0.\"ItemCode\") LIKE LOWER('%{safe_search}%')"
                f" OR LOWER(T0.\"ItemName\") LIKE LOWER('%{safe_search}%'))"
            )
        if produced_only:
            where_clause += f' AND T0."ItemCode" IN ({self._produced_items_sql()})'
        sql = """
            SELECT {top}
                T0."ItemCode",
                T0."ItemName",
                T0."InvntryUom" AS "UomCode"
            FROM "{schema}"."OITM" T0
            LEFT JOIN "{schema}"."OITB" G ON G."ItmsGrpCod" = T0."ItmsGrpCod"
            {where_clause}
            ORDER BY T0."ItemName" ASC
        """.format(
            schema=schema, top=f'TOP {int(limit)}' if limit else '', where_clause=where_clause,
        )
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to search SAP items: {e}")
            error = _caused_by(SAPReadError(f"Failed to search items: {e}"), e)
            if not produced_only:
                raise error  # only the run-startable items are copied
            return self._from_copy(error, lambda items: _copied_search(items, search, limit))

    def _produced_items_sql(self) -> str:
        """The run-startable items: a production BOM, in the FINISHED item group."""
        schema = self.client.context.config['hana']['schema']
        return (
            f'SELECT T9."ItemCode" FROM "{schema}"."OITM" T9'
            f' LEFT JOIN "{schema}"."OITB" G9 ON G9."ItmsGrpCod" = T9."ItmsGrpCod"'
            f' WHERE T9."ItemCode" IN (SELECT "Code" FROM "{schema}"."OITT")'
            f" AND UPPER(IFNULL(G9.\"ItmsGrpNam\", '')) = '{FINISHED_GOODS_ITEM_GROUP}'"
        )

    def _execute(self, sql: str) -> list:
        try:
            from sap_client.hana.connection import HanaConnection

            # Through HanaConnection for its fail-fast: while HANA is known to be
            # down a read goes to the copy at once, not after the connect timeout.
            connection = HanaConnection(self.client.context.hana).connect()
            cursor = connection.cursor()
            cursor.execute(sql)
            cols = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
            connection.close()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            raise SAPReadError(str(e)) from e


# ---------------------------------------------------------------------------
# answering from the BOM copy (rows: {"item", "bom", "pieces_per_case", "litres_per_piece"})
# ---------------------------------------------------------------------------

def _caused_by(error, cause):
    error.__cause__ = cause
    return error


def _copied_search(items, search, limit):
    term = (search or "").strip().lower()
    found = [
        row["item"] for row in items
        if not term
        or term in row["item"]["ItemCode"].lower()
        or term in (row["item"].get("ItemName") or "").lower()
    ]
    found.sort(key=lambda item: item.get("ItemName") or "")
    return found[:limit] if limit else found


def _copied_bom(items, item_code):
    for row in items:
        if row["item"]["ItemCode"] == item_code:
            return row["bom"]
    return None  # not a run-startable item the copy holds: say SAP is down


def _copied_map(items, codes, field):
    held = {row["item"]["ItemCode"]: row.get(field) for row in items}
    if not any(code in held for code in codes):
        return None
    return {code: held[code] for code in codes if held.get(code)}

"""The two SAP reads Tomorrow's run needs that no other board already makes.

Everything else it reads through the reader that already owns it — FG receipts
through the production check's ``ReconciliationReader``, packaging through the
Packing Material dashboard's reader, recipes through Planning & Purchase's
``get_bom_components`` — so a number here and the same number on its home
page cannot disagree.
"""

from collections import defaultdict
from typing import Dict, List, Sequence

from planning_purchase.hana_reader import HanaProductionPlanReader, _placeholders

# SAP transaction types (OINM.TransType)
GOODS_RECEIPT = 59        # finished goods received from production
SALES_OUT = (13, 15)      # A/R invoice and delivery: goods that left on a truck


class TomorrowRunReader(HanaProductionPlanReader):
    def room_stock(self, rooms: Sequence[str]) -> Dict[str, Dict[str, float]]:
        """Pieces on hand now, per room and item (``OITW.OnHand``)."""
        rows = self._rows(
            f"""
            SELECT W."WhsCode", W."ItemCode", W."OnHand"
            FROM "{self.schema}"."OITW" W
            WHERE W."WhsCode" IN ({_placeholders(len(rooms))})
              AND COALESCE(W."OnHand", 0) <> 0
            """,
            list(rooms),
        )
        out: Dict[str, Dict[str, float]] = {r: {} for r in rooms}
        for r in rows:
            out[r["WhsCode"]][r["ItemCode"]] = float(r["OnHand"] or 0)
        return out

    def room_flows(self, rooms: Sequence[str], date_from, date_to) -> List[Dict]:
        """Per day, room and item: pieces in, pieces out, production receipts, sales out.

        What left the two rooms on a day is measured off the rooms themselves:
        stock the night before + received from production − stock that night,
        which is production received − (in − out). Moves between the two rooms
        cancel out of it.
        """
        rows = self._rows(
            f"""
            SELECT
                O."DocDate", O."Warehouse", O."ItemCode",
                SUM(COALESCE(O."InQty", 0))  AS "InQty",
                SUM(COALESCE(O."OutQty", 0)) AS "OutQty",
                SUM(CASE WHEN O."TransType" = {GOODS_RECEIPT} THEN COALESCE(O."InQty", 0) ELSE 0 END) AS "Produced",
                SUM(CASE WHEN O."TransType" IN ({", ".join(str(t) for t in SALES_OUT)})
                         THEN COALESCE(O."OutQty", 0) ELSE 0 END) AS "SoldOut"
            FROM "{self.schema}"."OINM" O
            WHERE O."Warehouse" IN ({_placeholders(len(rooms))})
              AND O."DocDate" >= ? AND O."DocDate" <= ?
            GROUP BY O."DocDate", O."Warehouse", O."ItemCode"
            """,
            [*rooms, date_from, date_to],
        )
        return [
            {
                "date": r["DocDate"].date().isoformat() if hasattr(r["DocDate"], "date") else str(r["DocDate"])[:10],
                "room": r["Warehouse"], "code": r["ItemCode"],
                "in": float(r["InQty"] or 0), "out": float(r["OutQty"] or 0),
                "produced": float(r["Produced"] or 0), "sold_out": float(r["SoldOut"] or 0),
            }
            for r in rows
        ]


def left_per_day(flows: List[Dict], lp: Dict[str, float], production_room: str, truck_room: str):
    """Litres that left both rooms each day, and the truck room's own trucks."""
    by_day = defaultdict(lambda: {"received_l": 0.0, "net_l": 0.0, "truck_l": 0.0})
    for f in flows:
        litres = lp.get(f["code"]) or 0.0
        if not litres:
            continue
        d = by_day[f["date"]]
        d["net_l"] += (f["in"] - f["out"]) * litres
        if f["room"] == production_room:
            d["received_l"] += f["produced"] * litres
        if f["room"] == truck_room:
            d["truck_l"] += f["sold_out"] * litres
    return {
        day: {**v, "left_l": v["received_l"] - v["net_l"]}
        for day, v in by_day.items()
    }

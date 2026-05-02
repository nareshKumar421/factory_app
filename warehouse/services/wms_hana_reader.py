"""
warehouse/services/wms_hana_reader.py

SAP HANA reader for WMS stock overview, item details,
movement history, billing reconciliation, and warehouse summaries.
"""

import logging
from typing import Any, Dict, List, Optional

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class WMSHanaReader:
    """
    Reads stock, movement, and billing data from SAP HANA
    for the Warehouse Management System.
    """

    def __init__(self, company_code: str):
        self.context = CompanyContext(company_code)
        self.connection = HanaConnection(self.context.hana)
        self.schema = self.connection.schema

    # ==================================================================
    # Stock Overview
    # ==================================================================

    def get_stock_overview(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns paginated stock data with summary statistics.
        """
        query, params = self._build_stock_overview_query(filters)
        rows = self._execute(query, params)

        items = [self._map_stock_row(r) for r in rows]

        # Compute summary from full result set
        total_on_hand = sum(i["on_hand"] for i in items)
        total_committed = sum(i["committed"] for i in items)
        total_available = sum(i["available"] for i in items)
        total_value = sum(i["stock_value"] for i in items)

        # Pagination
        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))
        total_items = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        paginated_items = items[start:end]

        return {
            "summary": {
                "total_items": total_items,
                "total_on_hand": total_on_hand,
                "total_committed": total_committed,
                "total_available": total_available,
                "total_value": round(total_value, 2),
            },
            "items": paginated_items,
            "pagination": {
                "total": total_items,
                "page": page,
                "page_size": page_size,
                "pages": (total_items + page_size - 1) // page_size if total_items > 0 else 1,
            },
        }

    def _build_stock_overview_query(self, filters: Dict[str, Any]):
        clauses = []
        params = []

        # By default show items with some stock activity
        stock_filter = filters.get("stock_filter", "with_stock")
        if stock_filter == "with_stock":
            clauses.append('(T1."OnHand" <> 0 OR T1."IsCommited" <> 0 OR T1."OnOrder" <> 0)')
        elif stock_filter == "zero_stock":
            clauses.append('T1."OnHand" = 0')

        if filters.get("warehouse_code"):
            clauses.append('T1."WhsCode" = ?')
            params.append(filters["warehouse_code"])

        if filters.get("item_group"):
            clauses.append('T2."ItmsGrpNam" = ?')
            params.append(filters["item_group"])

        if filters.get("search"):
            search = f"%{filters['search']}%"
            clauses.append(
                '(T0."ItemCode" LIKE ? OR T0."ItemName" LIKE ?)'
            )
            params.extend([search, search])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        query = f"""
            SELECT
                T0."ItemCode",
                T0."ItemName",
                T2."ItmsGrpNam",
                IFNULL(T0."InvntryUom", '') AS "UoM",
                T1."WhsCode",
                T1."OnHand",
                T1."IsCommited",
                T1."OnOrder",
                (T1."OnHand" - T1."IsCommited") AS "Available",
                CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END,
                (T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "StockValue",
                T0."MinLevel",
                T0."MaxLevel",
                T0."LastPurPrc"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            LEFT JOIN "{self.schema}"."OITB" T2
                ON T0."ItmsGrpCod" = T2."ItmsGrpCod"
            {where}
            ORDER BY T1."WhsCode" ASC, T0."ItemCode" ASC
        """
        return query, params

    def _map_stock_row(self, row) -> Dict:
        on_hand = float(row[5] or 0)
        committed = float(row[6] or 0)
        on_order = float(row[7] or 0)
        available = float(row[8] or 0)
        avg_price = float(row[9] or 0)
        stock_value = float(row[10] or 0)
        min_level = float(row[11] or 0)
        max_level = float(row[12] or 0)

        # Determine stock status
        if on_hand == 0:
            stock_status = "ZERO"
        elif min_level > 0 and on_hand <= min_level * 0.5:
            stock_status = "CRITICAL"
        elif min_level > 0 and on_hand <= min_level:
            stock_status = "LOW"
        elif max_level > 0 and on_hand >= max_level:
            stock_status = "OVERSTOCK"
        else:
            stock_status = "NORMAL"

        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "item_group": row[2] or "",
            "uom": row[3] or "",
            "warehouse_code": row[4] or "",
            "on_hand": on_hand,
            "committed": committed,
            "on_order": on_order,
            "available": available,
            "avg_price": round(avg_price, 2),
            "stock_value": round(stock_value, 2),
            "min_level": min_level,
            "max_level": max_level,
            "last_purchase_price": float(row[13] or 0),
            "stock_status": stock_status,
        }

    # ==================================================================
    # Item Detail
    # ==================================================================

    def get_item_detail(self, item_code: str) -> Dict:
        """
        Returns detailed stock info for a single item across all warehouses.
        """
        query = f"""
            SELECT
                T0."ItemCode",
                T0."ItemName",
                T2."ItmsGrpNam",
                IFNULL(T0."InvntryUom", '') AS "UoM",
                CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END,
                T0."LastPurPrc",
                T0."MinLevel",
                T0."MaxLevel",
                T1."WhsCode",
                T1."OnHand",
                T1."IsCommited",
                T1."OnOrder",
                (T1."OnHand" - T1."IsCommited") AS "Available",
                (T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "StockValue"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            LEFT JOIN "{self.schema}"."OITB" T2
                ON T0."ItmsGrpCod" = T2."ItmsGrpCod"
            WHERE T0."ItemCode" = ?
            ORDER BY T1."WhsCode" ASC
        """
        rows = self._execute(query, [item_code])
        if not rows:
            return {"item": None, "warehouse_breakdown": [], "stock_summary": {}}

        first = rows[0]
        item = {
            "item_code": first[0],
            "item_name": first[1],
            "item_group": first[2] or "",
            "uom": first[3],
            "avg_price": float(first[4] or 0),
            "last_purchase_price": float(first[5] or 0),
            "min_level": float(first[6] or 0),
            "max_level": float(first[7] or 0),
        }

        warehouses = []
        total_on_hand = 0
        total_committed = 0
        total_available = 0
        total_value = 0

        for r in rows:
            on_hand = float(r[9] or 0)
            committed = float(r[10] or 0)
            available = float(r[12] or 0)
            value = float(r[13] or 0)

            if on_hand != 0 or committed != 0:
                warehouses.append({
                    "warehouse_code": r[8],
                    "on_hand": on_hand,
                    "committed": committed,
                    "on_order": float(r[11] or 0),
                    "available": available,
                    "value": round(value, 2),
                })
                total_on_hand += on_hand
                total_committed += committed
                total_available += available
                total_value += value

        return {
            "item": item,
            "warehouse_breakdown": warehouses,
            "stock_summary": {
                "total_on_hand": total_on_hand,
                "total_committed": total_committed,
                "total_available": total_available,
                "total_value": round(total_value, 2),
            },
        }

    # ==================================================================
    # Stock Movements (from OINM - Inventory Audit)
    # ==================================================================

    def get_stock_movements(self, filters: Dict[str, Any]) -> List[Dict]:
        """
        Returns stock movement history from the OINM table.
        """
        clauses = []
        params = []

        if filters.get("item_code"):
            clauses.append('T0."ItemCode" = ?')
            params.append(filters["item_code"])

        if filters.get("warehouse_code"):
            clauses.append('T0."Warehouse" = ?')
            params.append(filters["warehouse_code"])

        if filters.get("from_date"):
            clauses.append('T0."DocDate" >= ?')
            params.append(filters["from_date"])

        if filters.get("to_date"):
            clauses.append('T0."DocDate" <= ?')
            params.append(filters["to_date"])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(filters.get("limit", 100))

        query = f"""
            SELECT TOP {limit}
                T0."DocDate",
                T0."ItemCode",
                T1."ItemName",
                T0."Warehouse",
                T0."InQty",
                T0."OutQty",
                T0."TransType",
                IFNULL(T0."BASE_REF", '') AS "BaseRef",
                T0."TransNum",
                T0."CreatedBy"
            FROM "{self.schema}"."OINM" T0
            LEFT JOIN "{self.schema}"."OITM" T1
                ON T0."ItemCode" = T1."ItemCode"
            {where}
            ORDER BY T0."DocDate" DESC, T0."TransNum" DESC
        """
        rows = self._execute(query, params)
        return [self._map_movement_row(r) for r in rows]

    def _map_movement_row(self, row) -> Dict:
        in_qty = float(row[4] or 0)
        out_qty = float(row[5] or 0)
        trans_type = int(row[6] or 0)

        # Map SAP transaction types
        type_map = {
            13: "AR_INVOICE",
            14: "AR_CREDIT",
            15: "DELIVERY",
            16: "RETURN",
            18: "AP_INVOICE",
            19: "AP_CREDIT",
            20: "GRPO",
            21: "RETURN_TO_VENDOR",
            59: "GOODS_RECEIPT",
            60: "GOODS_ISSUE",
            67: "TRANSFER",
            202: "PRODUCTION_ORDER",
        }

        transaction_type = type_map.get(trans_type, f"OTHER_{trans_type}")
        direction = "IN" if in_qty > 0 else "OUT"
        quantity = in_qty if in_qty > 0 else out_qty

        return {
            "date": str(row[0]) if row[0] else "",
            "item_code": row[1] or "",
            "item_name": row[2] or "",
            "warehouse_code": row[3] or "",
            "in_qty": in_qty,
            "out_qty": out_qty,
            "quantity": quantity,
            "direction": direction,
            "transaction_type": transaction_type,
            "reference": row[7] or "",
            "doc_num": row[8] or "",
            "created_by": row[9] or "",
        }

    # ==================================================================
    # Warehouse Summary
    # ==================================================================

    def get_warehouse_summary(self) -> List[Dict]:
        """
        Returns summary metrics for each warehouse.
        """
        query = f"""
            SELECT
                T1."WhsCode",
                T2."WhsName",
                COUNT(DISTINCT T1."ItemCode") AS "ItemCount",
                SUM(T1."OnHand") AS "TotalOnHand",
                SUM(T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "TotalValue",
                SUM(CASE WHEN T0."MinLevel" > 0 AND T1."OnHand" <= T0."MinLevel" AND T1."OnHand" > 0 THEN 1 ELSE 0 END) AS "LowStockCount",
                SUM(CASE WHEN T0."MinLevel" > 0 AND T1."OnHand" <= T0."MinLevel" * 0.5 AND T1."OnHand" > 0 THEN 1 ELSE 0 END) AS "CriticalCount",
                SUM(CASE WHEN T0."MaxLevel" > 0 AND T1."OnHand" >= T0."MaxLevel" THEN 1 ELSE 0 END) AS "OverstockCount",
                SUM(CASE WHEN T1."OnHand" = 0 AND T0."MinLevel" > 0 THEN 1 ELSE 0 END) AS "ZeroStockCount"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            LEFT JOIN "{self.schema}"."OWHS" T2
                ON T1."WhsCode" = T2."WhsCode"
            WHERE T1."OnHand" <> 0 OR T1."IsCommited" <> 0
            GROUP BY T1."WhsCode", T2."WhsName"
            ORDER BY T1."WhsCode" ASC
        """
        rows = self._execute(query, [])
        return [
            {
                "warehouse_code": r[0] or "",
                "warehouse_name": r[1] or "",
                "total_items": int(r[2] or 0),
                "total_on_hand": float(r[3] or 0),
                "total_value": round(float(r[4] or 0), 2),
                "low_stock_count": int(r[5] or 0),
                "critical_stock_count": int(r[6] or 0),
                "overstock_count": int(r[7] or 0),
                "zero_stock_count": int(r[8] or 0),
            }
            for r in rows
        ]

    # ==================================================================
    # Dashboard Summary (KPIs + chart data)
    # ==================================================================

    def get_dashboard_summary(self, warehouse_code: Optional[str] = None) -> Dict:
        """
        Returns aggregated data for the WMS dashboard.
        """
        wh_filter = ""
        params = []
        if warehouse_code:
            wh_filter = 'AND T1."WhsCode" = ?'
            params.append(warehouse_code)

        # KPI summary
        kpi_query = f"""
            SELECT
                COUNT(DISTINCT T0."ItemCode") AS "TotalItems",
                SUM(T1."OnHand") AS "TotalOnHand",
                SUM(T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "TotalValue",
                SUM(CASE WHEN T0."MinLevel" > 0 AND T1."OnHand" <= T0."MinLevel" AND T1."OnHand" > 0 THEN 1 ELSE 0 END) AS "LowStock",
                SUM(CASE WHEN T0."MinLevel" > 0 AND T1."OnHand" <= T0."MinLevel" * 0.5 AND T1."OnHand" > 0 THEN 1 ELSE 0 END) AS "CriticalStock",
                SUM(CASE WHEN T1."OnHand" = 0 AND T0."MinLevel" > 0 THEN 1 ELSE 0 END) AS "ZeroStock",
                SUM(CASE WHEN T0."MaxLevel" > 0 AND T1."OnHand" >= T0."MaxLevel" THEN 1 ELSE 0 END) AS "Overstock"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            WHERE (T1."OnHand" <> 0 OR T1."IsCommited" <> 0)
            {wh_filter}
        """
        kpi_rows = self._execute(kpi_query, params)
        kpi = kpi_rows[0] if kpi_rows else [0] * 7

        # Stock by warehouse (for bar chart)
        wh_chart_query = f"""
            SELECT
                T1."WhsCode",
                COUNT(DISTINCT T1."ItemCode") AS "Items",
                SUM(T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "Value"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            WHERE T1."OnHand" <> 0
            {wh_filter}
            GROUP BY T1."WhsCode"
            ORDER BY "Value" DESC
        """
        wh_rows = self._execute(wh_chart_query, params)

        # Stock by item group (for pie chart)
        group_query = f"""
            SELECT
                IFNULL(T2."ItmsGrpNam", 'Ungrouped') AS "GroupName",
                COUNT(DISTINCT T0."ItemCode") AS "Items",
                SUM(T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "Value"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            LEFT JOIN "{self.schema}"."OITB" T2
                ON T0."ItmsGrpCod" = T2."ItmsGrpCod"
            WHERE T1."OnHand" <> 0
            {wh_filter}
            GROUP BY T2."ItmsGrpNam"
            ORDER BY "Value" DESC
        """
        group_rows = self._execute(group_query, params)

        # Top 10 items by value
        top_query = f"""
            SELECT TOP 10
                T0."ItemCode",
                T0."ItemName",
                SUM(T1."OnHand") AS "TotalQty",
                SUM(T1."OnHand" * CASE WHEN T0."AvgPrice" > 0 THEN T0."AvgPrice" ELSE T0."LastPurPrc" END) AS "TotalValue"
            FROM "{self.schema}"."OITM" T0
            INNER JOIN "{self.schema}"."OITW" T1
                ON T0."ItemCode" = T1."ItemCode"
            WHERE T1."OnHand" > 0
            {wh_filter}
            GROUP BY T0."ItemCode", T0."ItemName"
            ORDER BY "TotalValue" DESC
        """
        top_rows = self._execute(top_query, params)

        # Recent movements (last 20)
        recent_query = f"""
            SELECT TOP 20
                T0."DocDate",
                T0."ItemCode",
                T1."ItemName",
                T0."Warehouse",
                T0."InQty",
                T0."OutQty",
                T0."TransType"
            FROM "{self.schema}"."OINM" T0
            LEFT JOIN "{self.schema}"."OITM" T1
                ON T0."ItemCode" = T1."ItemCode"
            {"WHERE T0.\"Warehouse\" = ?" if warehouse_code else ""}
            ORDER BY T0."DocDate" DESC, T0."CreatedBy" DESC
        """
        recent_params = [warehouse_code] if warehouse_code else []
        recent_rows = self._execute(recent_query, recent_params)

        # Stock health distribution
        normal_count = int(kpi[0] or 0) - int(kpi[3] or 0) - int(kpi[5] or 0) - int(kpi[6] or 0)

        return {
            "kpis": {
                "total_items": int(kpi[0] or 0),
                "total_on_hand": float(kpi[1] or 0),
                "total_value": round(float(kpi[2] or 0), 2),
                "low_stock": int(kpi[3] or 0),
                "critical_stock": int(kpi[4] or 0),
                "zero_stock": int(kpi[5] or 0),
                "overstock": int(kpi[6] or 0),
            },
            "stock_by_warehouse": [
                {
                    "warehouse_code": r[0],
                    "items": int(r[1] or 0),
                    "value": round(float(r[2] or 0), 2),
                }
                for r in wh_rows
            ],
            "stock_by_group": [
                {
                    "group_name": r[0],
                    "items": int(r[1] or 0),
                    "value": round(float(r[2] or 0), 2),
                }
                for r in group_rows
            ],
            "top_items_by_value": [
                {
                    "item_code": r[0],
                    "item_name": r[1],
                    "quantity": float(r[2] or 0),
                    "value": round(float(r[3] or 0), 2),
                }
                for r in top_rows
            ],
            "stock_health": {
                "normal": max(normal_count, 0),
                "low": int(kpi[3] or 0),
                "critical": int(kpi[4] or 0),
                "zero": int(kpi[5] or 0),
                "overstock": int(kpi[6] or 0),
            },
            "recent_movements": [
                {
                    "date": str(r[0]) if r[0] else "",
                    "item_code": r[1] or "",
                    "item_name": r[2] or "",
                    "warehouse": r[3] or "",
                    "in_qty": float(r[4] or 0),
                    "out_qty": float(r[5] or 0),
                    "direction": "IN" if float(r[4] or 0) > 0 else "OUT",
                    "quantity": float(r[4] or 0) if float(r[4] or 0) > 0 else float(r[5] or 0),
                }
                for r in recent_rows
            ],
        }

    # ==================================================================
    # Billing Reconciliation
    # ==================================================================

    def get_billing_overview(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns billing reconciliation: GRPO received vs AP Invoice billed.
        """
        clauses = []
        params = []

        if filters.get("from_date"):
            clauses.append('T0."DocDate" >= ?')
            params.append(filters["from_date"])
        if filters.get("to_date"):
            clauses.append('T0."DocDate" <= ?')
            params.append(filters["to_date"])
        if filters.get("vendor"):
            clauses.append('T0."CardCode" = ?')
            params.append(filters["vendor"])
        if filters.get("warehouse_code"):
            clauses.append('T1."WhsCode" = ?')
            params.append(filters["warehouse_code"])

        where = f"AND {' AND '.join(clauses)}" if clauses else ""

        query = f"""
            SELECT
                T1."ItemCode",
                T1."Dscription" AS "ItemName",
                T1."WhsCode",
                SUM(T1."Quantity") AS "ReceivedQty",
                SUM(T1."LineTotal") AS "ReceivedValue",
                SUM(IFNULL(T1."Quantity", 0) - IFNULL(T1."OpenCreQty", 0)) AS "BilledQty",
                SUM(IFNULL(T1."LineTotal", 0) * (1 - IFNULL(T1."OpenCreQty", 0) / NULLIF(T1."Quantity", 0))) AS "BilledValue",
                SUM(T1."OpenCreQty") AS "UnbilledQty",
                MIN(T0."DocDate") AS "FirstGRPODate",
                MAX(T0."DocDate") AS "LastGRPODate"
            FROM "{self.schema}"."OPDN" T0
            INNER JOIN "{self.schema}"."PDN1" T1
                ON T0."DocEntry" = T1."DocEntry"
            WHERE T0."CANCELED" = 'N'
            {where}
            GROUP BY T1."ItemCode", T1."Dscription", T1."WhsCode"
            ORDER BY SUM(T1."OpenCreQty") DESC
        """
        rows = self._execute(query, params)

        items = []
        total_received_qty = 0
        total_billed_qty = 0
        total_unbilled_qty = 0
        total_received_value = 0
        total_billed_value = 0

        for r in rows:
            received_qty = float(r[3] or 0)
            received_value = float(r[4] or 0)
            billed_qty = float(r[5] or 0)
            billed_value = float(r[6] or 0)
            unbilled_qty = float(r[7] or 0)

            if received_qty == 0:
                billing_status = "UNBILLED"
            elif unbilled_qty <= 0:
                billing_status = "FULLY_BILLED"
            elif billed_qty > 0:
                billing_status = "PARTIALLY_BILLED"
            else:
                billing_status = "UNBILLED"

            total_received_qty += received_qty
            total_billed_qty += billed_qty
            total_unbilled_qty += unbilled_qty
            total_received_value += received_value
            total_billed_value += billed_value

            items.append({
                "item_code": r[0] or "",
                "item_name": r[1] or "",
                "warehouse_code": r[2] or "",
                "received_qty": received_qty,
                "received_value": round(received_value, 2),
                "billed_qty": billed_qty,
                "billed_value": round(billed_value, 2),
                "unbilled_qty": unbilled_qty,
                "unbilled_value": round(received_value - billed_value, 2),
                "status": billing_status,
                "first_grpo_date": str(r[8]) if r[8] else "",
                "last_grpo_date": str(r[9]) if r[9] else "",
            })

        return {
            "summary": {
                "total_received_qty": total_received_qty,
                "total_billed_qty": total_billed_qty,
                "total_unbilled_qty": total_unbilled_qty,
                "total_received_value": round(total_received_value, 2),
                "total_billed_value": round(total_billed_value, 2),
                "total_unbilled_value": round(total_received_value - total_billed_value, 2),
                "fully_billed_count": sum(1 for i in items if i["status"] == "FULLY_BILLED"),
                "partially_billed_count": sum(1 for i in items if i["status"] == "PARTIALLY_BILLED"),
                "unbilled_count": sum(1 for i in items if i["status"] == "UNBILLED"),
            },
            "items": items,
        }

    # ==================================================================
    # Stock Transfers (OWTR/WTR1)
    # ==================================================================

    def get_transfer_overview(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns stock transfer lines and route analytics from SAP.
        """
        clauses = ['T0."CANCELED" = ?']
        params = ["N"]

        if filters.get("from_date"):
            clauses.append('T0."DocDate" >= ?')
            params.append(filters["from_date"])
        if filters.get("to_date"):
            clauses.append('T0."DocDate" <= ?')
            params.append(filters["to_date"])
        if filters.get("from_warehouse"):
            clauses.append('T1."FromWhsCod" = ?')
            params.append(filters["from_warehouse"])
        if filters.get("to_warehouse"):
            clauses.append('T1."WhsCode" = ?')
            params.append(filters["to_warehouse"])
        if filters.get("item_code"):
            clauses.append('T1."ItemCode" = ?')
            params.append(filters["item_code"])

        limit = int(filters.get("limit", 200))
        where = f"WHERE {' AND '.join(clauses)}"

        query = f"""
            SELECT TOP {limit}
                T0."DocEntry",
                T0."DocNum",
                T0."DocDate",
                T0."Filler",
                T0."ToWhsCode",
                IFNULL(T0."Comments", '') AS "Comments",
                T1."LineNum",
                T1."ItemCode",
                IFNULL(T1."Dscription", T2."ItemName") AS "ItemName",
                T1."Quantity",
                T1."FromWhsCod",
                T1."WhsCode"
            FROM "{self.schema}"."OWTR" T0
            INNER JOIN "{self.schema}"."WTR1" T1
                ON T0."DocEntry" = T1."DocEntry"
            LEFT JOIN "{self.schema}"."OITM" T2
                ON T1."ItemCode" = T2."ItemCode"
            {where}
            ORDER BY T0."DocDate" DESC, T0."DocNum" DESC, T1."LineNum" ASC
        """
        rows = self._execute(query, params)
        transfers = [self._map_transfer_row(r) for r in rows]

        route_map = {}
        docs = set()
        total_qty = 0.0
        for transfer in transfers:
            docs.add(transfer["doc_entry"])
            total_qty += transfer["quantity"]
            route_key = (transfer["from_warehouse"], transfer["to_warehouse"])
            route = route_map.setdefault(
                route_key,
                {
                    "from_warehouse": transfer["from_warehouse"],
                    "to_warehouse": transfer["to_warehouse"],
                    "transfer_count": 0,
                    "line_count": 0,
                    "quantity": 0.0,
                },
            )
            route["line_count"] += 1
            route["quantity"] += transfer["quantity"]

        for route in route_map.values():
            route_docs = {
                t["doc_entry"]
                for t in transfers
                if t["from_warehouse"] == route["from_warehouse"]
                and t["to_warehouse"] == route["to_warehouse"]
            }
            route["transfer_count"] = len(route_docs)
            route["quantity"] = round(route["quantity"], 3)

        routes = sorted(
            route_map.values(),
            key=lambda r: (r["line_count"], r["quantity"]),
            reverse=True,
        )

        return {
            "summary": {
                "transfer_count": len(docs),
                "line_count": len(transfers),
                "total_quantity": round(total_qty, 3),
                "route_count": len(routes),
            },
            "routes": routes[:20],
            "transfers": transfers,
        }

    def _map_transfer_row(self, row) -> Dict:
        return {
            "doc_entry": int(row[0] or 0),
            "doc_num": int(row[1] or 0),
            "doc_date": str(row[2]) if row[2] else "",
            "header_from_warehouse": row[3] or "",
            "header_to_warehouse": row[4] or "",
            "comments": row[5] or "",
            "line_num": int(row[6] or 0),
            "item_code": row[7] or "",
            "item_name": row[8] or "",
            "quantity": float(row[9] or 0),
            "from_warehouse": row[10] or "",
            "to_warehouse": row[11] or "",
        }

    # ==================================================================
    # Batch Expiry / FEFO (OBTN/OBTQ)
    # ==================================================================

    def get_batch_expiry_overview(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns batch stock with expiry status from SAP batch tables.
        """
        clauses = ['T2."Quantity" > 0']
        params = []

        if filters.get("warehouse_code"):
            clauses.append('T2."WhsCode" = ?')
            params.append(filters["warehouse_code"])
        if filters.get("item_code"):
            clauses.append('T0."ItemCode" = ?')
            params.append(filters["item_code"])
        if filters.get("search"):
            search = f"%{filters['search']}%"
            clauses.append(
                '(T0."ItemCode" LIKE ? OR T1."ItemName" LIKE ? OR T0."DistNumber" LIKE ?)'
            )
            params.extend([search, search, search])
        if filters.get("days_to_expiry"):
            clauses.append('T0."ExpDate" <= ADD_DAYS(CURRENT_DATE, ?)')
            params.append(int(filters["days_to_expiry"]))

        limit = int(filters.get("limit", 300))
        where = f"WHERE {' AND '.join(clauses)}"

        query = f"""
            SELECT TOP {limit}
                T0."ItemCode",
                T1."ItemName",
                T0."DistNumber",
                T0."ExpDate",
                T0."MnfDate",
                T0."Status",
                T2."WhsCode",
                T2."Quantity",
                DAYS_BETWEEN(CURRENT_DATE, T0."ExpDate") AS "DaysToExpiry"
            FROM "{self.schema}"."OBTN" T0
            INNER JOIN "{self.schema}"."OITM" T1
                ON T0."ItemCode" = T1."ItemCode"
            INNER JOIN "{self.schema}"."OBTQ" T2
                ON T0."AbsEntry" = T2."MdAbsEntry"
                AND T0."ItemCode" = T2."ItemCode"
            {where}
            ORDER BY T0."ExpDate" ASC, T0."ItemCode" ASC, T2."WhsCode" ASC
        """
        rows = self._execute(query, params)
        batches = [self._map_batch_row(r) for r in rows]

        status_filter = filters.get("expiry_status", "")
        if status_filter:
            batches = [b for b in batches if b["expiry_status"] == status_filter]

        summary = {
            "batch_count": len(batches),
            "expired_count": sum(1 for b in batches if b["expiry_status"] == "EXPIRED"),
            "critical_count": sum(1 for b in batches if b["expiry_status"] == "CRITICAL"),
            "warning_count": sum(1 for b in batches if b["expiry_status"] == "WARNING"),
            "ok_count": sum(1 for b in batches if b["expiry_status"] == "OK"),
            "total_quantity": round(sum(b["quantity"] for b in batches), 3),
        }

        return {
            "summary": summary,
            "batches": batches,
        }

    def _map_batch_row(self, row) -> Dict:
        days_to_expiry = row[8]
        if days_to_expiry is None:
            expiry_status = "NO_EXPIRY"
            days_value = None
        else:
            days_value = int(days_to_expiry)
            if days_value < 0:
                expiry_status = "EXPIRED"
            elif days_value <= 30:
                expiry_status = "CRITICAL"
            elif days_value <= 90:
                expiry_status = "WARNING"
            else:
                expiry_status = "OK"

        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "batch_number": row[2] or "",
            "expiry_date": str(row[3]) if row[3] else "",
            "manufacturing_date": str(row[4]) if row[4] else "",
            "sap_status": str(row[5]) if row[5] is not None else "",
            "warehouse_code": row[6] or "",
            "quantity": float(row[7] or 0),
            "days_to_expiry": days_value,
            "expiry_status": expiry_status,
        }

    # ==================================================================
    # Sales Order Backlog (ORDR/RDR1)
    # ==================================================================

    def get_sales_order_backlog(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns open sales-order lines that can feed WMS picking.
        """
        clauses = ['T0."DocStatus" = ?', 'T1."LineStatus" = ?', 'T1."OpenQty" > 0']
        params = ["O", "O"]

        if filters.get("warehouse_code"):
            clauses.append('T1."WhsCode" = ?')
            params.append(filters["warehouse_code"])
        if filters.get("from_due_date"):
            clauses.append('T0."DocDueDate" >= ?')
            params.append(filters["from_due_date"])
        if filters.get("to_due_date"):
            clauses.append('T0."DocDueDate" <= ?')
            params.append(filters["to_due_date"])
        if filters.get("search"):
            search = f"%{filters['search']}%"
            clauses.append(
                '(T0."CardCode" LIKE ? OR T0."CardName" LIKE ? OR T1."ItemCode" LIKE ? OR T1."Dscription" LIKE ?)'
            )
            params.extend([search, search, search, search])

        limit = int(filters.get("limit", 300))
        where = f"WHERE {' AND '.join(clauses)}"

        query = f"""
            SELECT TOP {limit}
                T0."DocEntry",
                T0."DocNum",
                T0."DocDate",
                T0."DocDueDate",
                T0."CardCode",
                T0."CardName",
                T1."LineNum",
                T1."ItemCode",
                T1."Dscription",
                T1."WhsCode",
                T1."Quantity",
                T1."OpenQty",
                IFNULL(T1."DelivrdQty", 0) AS "DeliveredQty"
            FROM "{self.schema}"."ORDR" T0
            INNER JOIN "{self.schema}"."RDR1" T1
                ON T0."DocEntry" = T1."DocEntry"
            {where}
            ORDER BY T0."DocDueDate" ASC, T0."DocNum" ASC, T1."LineNum" ASC
        """
        rows = self._execute(query, params)
        lines = [self._map_sales_order_backlog_row(r) for r in rows]

        docs = {line["doc_entry"] for line in lines}
        warehouse_map = {}
        for line in lines:
            wh = warehouse_map.setdefault(
                line["warehouse_code"],
                {
                    "warehouse_code": line["warehouse_code"],
                    "order_count": 0,
                    "line_count": 0,
                    "open_quantity": 0.0,
                },
            )
            wh["line_count"] += 1
            wh["open_quantity"] += line["open_qty"]

        for wh in warehouse_map.values():
            wh_docs = {
                line["doc_entry"]
                for line in lines
                if line["warehouse_code"] == wh["warehouse_code"]
            }
            wh["order_count"] = len(wh_docs)
            wh["open_quantity"] = round(wh["open_quantity"], 3)

        warehouses = sorted(
            warehouse_map.values(),
            key=lambda item: (item["open_quantity"], item["line_count"]),
            reverse=True,
        )

        return {
            "summary": {
                "order_count": len(docs),
                "line_count": len(lines),
                "open_quantity": round(sum(line["open_qty"] for line in lines), 3),
                "warehouse_count": len(warehouses),
            },
            "warehouses": warehouses,
            "lines": lines,
        }

    def _map_sales_order_backlog_row(self, row) -> Dict:
        ordered_qty = float(row[10] or 0)
        open_qty = float(row[11] or 0)
        delivered_qty = float(row[12] or 0)
        fulfillment_pct = 0.0
        if ordered_qty > 0:
            fulfillment_pct = round((delivered_qty / ordered_qty) * 100, 2)

        return {
            "doc_entry": int(row[0] or 0),
            "doc_num": int(row[1] or 0),
            "doc_date": str(row[2]) if row[2] else "",
            "due_date": str(row[3]) if row[3] else "",
            "customer_code": row[4] or "",
            "customer_name": row[5] or "",
            "line_num": int(row[6] or 0),
            "item_code": row[7] or "",
            "item_name": row[8] or "",
            "warehouse_code": row[9] or "",
            "ordered_qty": ordered_qty,
            "open_qty": open_qty,
            "delivered_qty": delivered_qty,
            "fulfillment_pct": fulfillment_pct,
        }

    # ==================================================================
    # Warehouse List (for dropdowns)
    # ==================================================================

    def get_warehouses(self) -> List[Dict]:
        """Returns list of active warehouses."""
        query = f"""
            SELECT "WhsCode", "WhsName"
            FROM "{self.schema}"."OWHS"
            WHERE "Inactive" = 'N'
            ORDER BY "WhsCode" ASC
        """
        rows = self._execute(query, [])
        return [{"code": r[0], "name": r[1] or r[0]} for r in rows]

    # ==================================================================
    # Item Groups (for filter dropdowns)
    # ==================================================================

    def get_item_groups(self) -> List[Dict]:
        """Returns list of item groups."""
        query = f"""
            SELECT "ItmsGrpCod", "ItmsGrpNam"
            FROM "{self.schema}"."OITB"
            ORDER BY "ItmsGrpNam" ASC
        """
        rows = self._execute(query, [])
        return [{"code": int(r[0]), "name": r[1] or ""} for r in rows]

    # ==================================================================
    # Execution Helper
    # ==================================================================

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error(f"WMS HANA query error: {e}")
            raise SAPDataError(
                "Failed to retrieve WMS data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"WMS HANA data error: {e}")
            raise SAPDataError(
                "Failed to retrieve WMS data from SAP. Please try again."
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

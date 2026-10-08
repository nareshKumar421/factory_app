NORMALIZED_COLUMN_ANALYSIS = [
    {
        "column": "company_code",
        "postgres_type": "varchar(50)",
        "business_meaning": "Factory company context used to call the SAP HANA procedure.",
    },
    {
        "column": "forecast_id",
        "postgres_type": "integer nullable",
        "business_meaning": "SAP forecast master AbsID used by the procedure when available.",
    },
    {
        "column": "forecast_name",
        "postgres_type": "varchar(255)",
        "business_meaning": "SAP forecast or planning cycle selected for the refresh.",
    },
    {
        "column": "planning_month",
        "postgres_type": "varchar(255)",
        "business_meaning": "Planning month/name returned by the procedure or inferred from forecast.",
    },
    {
        "column": "item_code",
        "postgres_type": "varchar(100)",
        "business_meaning": "SAP item code for finished goods or BOM-driven component requirement.",
    },
    {
        "column": "item_name",
        "postgres_type": "varchar(500)",
        "business_meaning": "SAP item description.",
    },
    {
        "column": "planned_qty",
        "postgres_type": "numeric(24,6) nullable",
        "business_meaning": "Forecasted sales/production planning quantity where the procedure returns it.",
    },
    {
        "column": "base_required_qty",
        "postgres_type": "numeric(24,6) nullable",
        "business_meaning": "Base requirement calculated from forecast and BOM before final stock adjustment.",
    },
    {
        "column": "min_stock",
        "postgres_type": "numeric(24,6)",
        "business_meaning": "Minimum stock benchmark to preserve while planning procurement/production.",
    },
    {
        "column": "stock_in_hand",
        "postgres_type": "numeric(24,6)",
        "business_meaning": "Current SAP stock quantity in the warehouses used by the procedure.",
    },
    {
        "column": "required_qty",
        "postgres_type": "numeric(24,6)",
        "business_meaning": "Final requirement gap after considering planned/base requirement, min stock, and stock in hand.",
    },
    {
        "column": "received_qty",
        "postgres_type": "numeric(24,6) nullable",
        "business_meaning": "Quantity received on posted GRPOs (OPDN/PDN1, inventory unit) within the forecast dates.",
    },
    {
        "column": "open_po_qty",
        "postgres_type": "numeric(24,6)",
        "business_meaning": "Open purchase order quantity already covering the requirement where returned.",
    },
    {
        "column": "net_shortage_qty",
        "postgres_type": "numeric(24,6)",
        "business_meaning": "Requirement still uncovered after subtracting open purchase orders.",
    },
    {
        "column": "report_execution_at",
        "postgres_type": "timestamp with time zone nullable",
        "business_meaning": "SAP report timestamp returned by the procedure, or refresh timestamp fallback.",
    },
    {
        "column": "raw_payload",
        "postgres_type": "jsonb",
        "business_meaning": "Original procedure row retained for auditability and future column changes.",
    },
]


PROCEDURE_OUTPUT_ANALYSIS = {
    "JIVO_BEVERAGES": [
        {
            "hana_column": "Planning Month",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "planning_month",
            "business_meaning": "Forecast planning month/name from OFCT.",
        },
        {
            "hana_column": "ItemCode",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "item_code",
            "business_meaning": "Finished good or BOM component item code.",
        },
        {
            "hana_column": "ItemName",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "item_name",
            "business_meaning": "SAP item description.",
        },
        {
            "hana_column": "Planned Qty",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "planned_qty",
            "business_meaning": "Forecast line quantity from FCT1.",
        },
        {
            "hana_column": "Min Stock",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "min_stock",
            "business_meaning": "Warehouse minimum stock benchmark.",
        },
        {
            "hana_column": "Stock In Hand",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "stock_in_hand",
            "business_meaning": "Current inventory balance in configured warehouses.",
        },
        {
            "hana_column": "Required Qty",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "required_qty",
            "business_meaning": "Calculated quantity required after stock adjustment.",
        },
        {
            "hana_column": "Open PO Quantity",
            "hana_type": "DECIMAL(28,6)",
            "mapped_column": "open_po_qty",
            "business_meaning": "Open PO quantity for the same item.",
        },
        {
            "hana_column": "Report Execution Date & Time",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "report_execution_at",
            "business_meaning": "SAP procedure execution timestamp.",
        },
    ],
    "JIVO_OIL": [
        {
            "hana_column": "Planning Month",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "planning_month",
            "business_meaning": "Forecast planning month/name from OFCT.",
        },
        {
            "hana_column": "ItemCode",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "item_code",
            "business_meaning": "Finished good or BOM component item code.",
        },
        {
            "hana_column": "ItemName",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "item_name",
            "business_meaning": "SAP item description.",
        },
        {
            "hana_column": "Planned Qty",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "planned_qty",
            "business_meaning": "Forecast line quantity from FCT1.",
        },
        {
            "hana_column": "Min Stock",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "min_stock",
            "business_meaning": "Warehouse minimum stock benchmark.",
        },
        {
            "hana_column": "Stock In Hand",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "stock_in_hand",
            "business_meaning": "Current inventory balance in configured warehouses.",
        },
        {
            "hana_column": "Required Qty",
            "hana_type": "DECIMAL(19,4)",
            "mapped_column": "required_qty",
            "business_meaning": "Calculated quantity required after stock adjustment.",
        },
        {
            "hana_column": "Open PO Quantity",
            "hana_type": "DECIMAL(28,6)",
            "mapped_column": "open_po_qty",
            "business_meaning": "Open PO quantity for the same item.",
        },
        {
            "hana_column": "Report Execution Date & Time",
            "hana_type": "NVARCHAR(999)",
            "mapped_column": "report_execution_at",
            "business_meaning": "SAP procedure execution timestamp.",
        },
    ],
}

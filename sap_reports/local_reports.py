"""
sap_reports/local_reports.py

Reports authored in this app, offered through the same catalogue, screen,
filters, exports and access rules as the queries mirrored from SAP.

The sync can only discover what lives in ``OUQR`` -- SAP's saved queries. Some
reports the teams rely on live elsewhere: the warehouse stock-audit sheet is a
Crystal Report (``RDOC`` code RCRI0010, "Inventory Audit Report Manual") over a
HANA procedure, invisible to Query Manager. A local report carries such SQL in
this file instead: seeded by ``manage.py seed_local_sap_reports``, flagged
``is_local`` so a sync neither refreshes it nor marks it missing, and shown in
the SQL tab exactly as written here -- what you read is what runs.

The contract mirrors the sync's: this file owns the SQL (a re-seed refreshes it
and re-infers the prompts), while friendly names, descriptions and corrected
parameter labels belong to the people running the app and survive every
re-seed.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from .models import SapReport
from .services.catalog import sync_report_parameters
from .sql import detect_statement_kind, is_runnable, normalise_sql, sql_hash

# Local reports are keyed like SAP's own system queries -- by a negative
# internal key -- but far outside the range SAP uses (-3 .. -32), so the
# per-company unique constraint keeps working and no OUQR row can collide.
LOCAL_INTERNAL_KEY_BASE = -900000

# Their category, shown as the badge on the report page and the group heading
# on the list. It never exists inside SAP, so no sync ever claims it.
LOCAL_CATEGORY_ID = -1
LOCAL_CATEGORY_NAME = "Factory App"


# ---------------------------------------------------------------------------
# Inventory Audit Report
# ---------------------------------------------------------------------------
#
# A rebuild of the stock-audit sheet the warehouse teams run inside the SAP
# client as "Inventory Audit Report Manual" (Modules > Inventory > Inventory
# Reports). The original is a Crystal Report calling the HANA procedure
# "REPORT Inventory Audit Report Manual"(FROMDATE, TODATE); this SQL keeps its
# shape -- an opening-balance row per item and godown, then every document
# that moved stock in the period, quantities only -- with three deliberate
# changes:
#
#   1. Box / Loose Qty use FLOOR and MOD. The procedure computed loose units
#      as OnHand - OnHand/SalFactor2, which is not a remainder.
#   2. Box / Loose Qty are split from stock as on the To date, computed from
#      OINM. The procedure read OITW."OnHand" -- today's stock -- whatever
#      dates were asked for.
#   3. Item and Warehouse are real, optional prompts. The Crystal report only
#      filtered them client-side after fetching everything.
#
# The split still applies only to finished goods (item group 102, "FINISHED"
# in every company database) with a pieces-per-box factor, as the procedure
# had it.
INVENTORY_AUDIT_SQL = """\
WITH "SCOPE" AS (
    -- item/godown pairs whose stock actually changed inside the period
    SELECT N."ItemCode", N."Warehouse"
    FROM OINM N
    WHERE N."DocDate" BETWEEN '[%0]' AND '[%1]'
      AND (N."ItemCode" = '[%2]' OR '[%2]' = '')
      AND (N."Warehouse" = '[%3]' OR '[%3]' = '')
    GROUP BY N."ItemCode", N."Warehouse"
    HAVING SUM(N."InQty" - N."OutQty") <> 0
),
"AS_ON" AS (
    -- stock on hand as on the To date, for the Box / Loose split
    SELECT N."ItemCode", N."Warehouse", SUM(N."InQty" - N."OutQty") AS "OnHand"
    FROM OINM N
    WHERE N."DocDate" <= '[%1]'
      AND (N."ItemCode" = '[%2]' OR '[%2]' = '')
      AND (N."Warehouse" = '[%3]' OR '[%3]' = '')
    GROUP BY N."ItemCode", N."Warehouse"
)
SELECT * FROM (
    -- every document that moved stock inside the period
    SELECT
        M."Warehouse" AS "Godown",
        M."ItemCode",
        I."ItemName",
        I."U_Sub_Group" AS "Variety",
        CAST(M."DocDate" AS DATE) AS "DocDate",
        M."DocTime",
        CASE M."TransType"
            WHEN 13 THEN 'IN' WHEN 14 THEN 'CN' WHEN 15 THEN 'DL' WHEN 16 THEN 'RE'
            WHEN 18 THEN 'PU' WHEN 19 THEN 'PT' WHEN 20 THEN 'PD' WHEN 21 THEN 'PR'
            WHEN 59 THEN 'SI' WHEN 60 THEN 'SO' WHEN 67 THEN 'IM' WHEN 10000071 THEN 'ST'
            ELSE 'OT'
        END || '-' || IFNULL(M."BASE_REF", '') AS "DocNum",
        I."SalPackMsr" AS "UOM",
        CAST(SUM(M."InQty" - M."OutQty") AS DECIMAL(19,2)) AS "Quantity",
        CASE WHEN I."ItmsGrpCod" = 102 AND IFNULL(I."SalFactor2", 0) > 0
             THEN CAST(FLOOR(A."OnHand" / I."SalFactor2") AS INTEGER) ELSE 0 END AS "Box",
        CASE WHEN I."ItmsGrpCod" = 102 AND IFNULL(I."SalFactor2", 0) > 0
             THEN CAST(MOD(A."OnHand", I."SalFactor2") AS INTEGER) ELSE 0 END AS "Loose Qty"
    FROM OINM M
    INNER JOIN "SCOPE" S ON S."ItemCode" = M."ItemCode" AND S."Warehouse" = M."Warehouse"
    INNER JOIN OITM I ON I."ItemCode" = M."ItemCode"
    LEFT JOIN "AS_ON" A ON A."ItemCode" = M."ItemCode" AND A."Warehouse" = M."Warehouse"
    WHERE M."DocDate" BETWEEN '[%0]' AND '[%1]'
    GROUP BY M."Warehouse", M."ItemCode", I."ItemName", I."U_Sub_Group", I."SalPackMsr",
             M."DocDate", M."DocTime", M."TransType", M."BASE_REF",
             I."ItmsGrpCod", I."SalFactor2", A."OnHand"

    UNION ALL

    -- opening balance as on the From date, one row per item and godown
    SELECT
        N."Warehouse" AS "Godown",
        N."ItemCode",
        I."ItemName",
        I."U_Sub_Group" AS "Variety",
        TO_DATE('[%0]', 'YYYYMMDD') AS "DocDate",
        0 AS "DocTime",
        'OB' AS "DocNum",
        I."SalPackMsr" AS "UOM",
        CAST(SUM(N."InQty" - N."OutQty") AS DECIMAL(19,2)) AS "Quantity",
        CASE WHEN I."ItmsGrpCod" = 102 AND IFNULL(I."SalFactor2", 0) > 0
             THEN CAST(FLOOR(A."OnHand" / I."SalFactor2") AS INTEGER) ELSE 0 END AS "Box",
        CASE WHEN I."ItmsGrpCod" = 102 AND IFNULL(I."SalFactor2", 0) > 0
             THEN CAST(MOD(A."OnHand", I."SalFactor2") AS INTEGER) ELSE 0 END AS "Loose Qty"
    FROM OINM N
    INNER JOIN OITM I ON I."ItemCode" = N."ItemCode"
    LEFT JOIN "AS_ON" A ON A."ItemCode" = N."ItemCode" AND A."Warehouse" = N."Warehouse"
    WHERE N."DocDate" < '[%0]'
      AND (N."ItemCode" = '[%2]' OR '[%2]' = '')
      AND (N."Warehouse" = '[%3]' OR '[%3]' = '')
    GROUP BY N."Warehouse", N."ItemCode", I."ItemName", I."U_Sub_Group", I."SalPackMsr",
             I."ItmsGrpCod", I."SalFactor2", A."OnHand"
) R
WHERE R."Quantity" <> 0
ORDER BY R."Godown", R."ItemCode", R."DocDate", R."DocTime"
"""


@dataclass(frozen=True)
class LocalReport:
    """One app-authored report, ready to be seeded into the catalogue."""

    internal_key: int
    slug: str
    sap_name: str
    description: str
    sql_text: str
    row_limit: Optional[int] = None


LOCAL_REPORTS = [
    LocalReport(
        internal_key=LOCAL_INTERNAL_KEY_BASE - 1,
        slug="inventory-audit-report",
        sap_name="Inventory Audit Report",
        description=(
            "Stock audit sheet per godown and item: the opening balance as on "
            "the From date, every document that moved stock in the period, and "
            "the box / loose split of finished-goods stock as on the To date. "
            "A rebuild of SAP's \"Inventory Audit Report Manual\" with the box "
            "arithmetic corrected. Run it for one day (From = To) to audit "
            "today's stock."
        ),
        sql_text=INVENTORY_AUDIT_SQL,
    ),
]


def seed_local_reports(company) -> Dict[str, List[str]]:
    """
    Registers (or refreshes) every local report for one company.

    Safe to repeat: an unchanged report is left alone; a changed one has its
    SQL refreshed and its prompts re-inferred, keeping any parameter a person
    customised -- the same contract the SAP sync honours.
    """
    summary = {"company": company.code, "created": [], "updated": [], "unchanged": []}
    with transaction.atomic():
        for definition in LOCAL_REPORTS:
            report, outcome = _seed_one(company, definition)
            summary[outcome].append(report.title)
    return summary


def _seed_one(company, definition: LocalReport) -> Tuple[SapReport, str]:
    sql_text = normalise_sql(definition.sql_text)
    new_hash = sql_hash(sql_text)
    runnable, reason = is_runnable(sql_text)

    report = SapReport.objects.filter(
        company=company,
        sap_internal_key=definition.internal_key,
    ).first()
    is_new = report is None

    if is_new:
        report = SapReport(
            company=company,
            sap_internal_key=definition.internal_key,
            slug=_unique_slug(company, definition.slug),
            description=definition.description,
            row_limit=definition.row_limit,
        )

    sql_changed = is_new or report.sql_hash != new_hash

    report.is_local = True
    report.sap_name = definition.sap_name
    report.sap_category_id = LOCAL_CATEGORY_ID
    report.sap_category_name = LOCAL_CATEGORY_NAME
    report.sql_text = sql_text
    report.sql_hash = new_hash
    report.statement_kind = detect_statement_kind(sql_text)
    report.is_runnable = runnable
    report.not_runnable_reason = "" if runnable else reason[:255]
    report.is_missing_in_sap = False
    report.last_synced_at = timezone.now()
    report.save()

    if sql_changed:
        sync_report_parameters(report)

    if is_new:
        return report, "created"
    return report, "updated" if sql_changed else "unchanged"


def _unique_slug(company, base: str) -> str:
    """The wanted slug, suffixed only if a synced report already claimed it."""
    slug = base
    suffix = 2
    while SapReport.objects.filter(company=company, slug=slug).exists():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug

"""
pm_demand/constants.py

Every SAP fact this dashboard rests on, in one place, with the evidence.

WHAT COUNTS AS PACKING MATERIAL
-------------------------------
``OITM.ItmsGrpCod`` = 105, whose ``OITB.ItmsGrpNam`` is 'PACKAGING MATERIAL'.
Verified as code 105 in all three company schemas (Oil holds 877 items under
it), so the code is safe to use directly -- but the group NAME is what SAP
enforces, so the reader checks that the name and the code still agree and says
so in the response rather than trusting the number blind. An item-code prefix
is only a naming convention and is never matched on.

WHAT COUNTS AS CONSUMPTION
--------------------------
``OINM.TransType`` = 60 (goods issue) with ``OutQty`` out of the production
warehouse. This is the load-bearing choice of the whole module, and it is NOT
what ``production_execution.reconciliation_reader`` does -- that reads
TransType 67 *into* BH-PC, which is the store-to-line transfer. The two
disagree badly wherever the factory makes its own packing material.
August 2026, Oil:

    PM0000121  PET BOTTLE 1 LTR 52 GMS POMACE
        transferred into BH-PC (67 InQty)             2
        blown in-house into BH-PC (59 InQty)    662,769
        issued out of BH-PC (60 OutQty)         603,505   <- what was used

    PM0000194  PET BOTTLE 1 LTR 40 GMS
        transferred into BH-PC (67 InQty)       153,890
        blown in-house into BH-PC (59 InQty)    195,891
        issued out of BH-PC (60 OutQty)         260,576   <- what was used

Reading the transfer would have reported 2 bottles against 603,505 really
consumed. TransType 60 is the only figure that answers "what did the line use".

THE PRODUCTION WAREHOUSE IS PER COMPANY
---------------------------------------
Oil issues from BH-PC 'Bhakharpur Production Consumption'; Beverages issues
from BH-PP 'Bhakharpur Production Process 1st Floor' (BH-PP is flagged
Inactive in Oil's own schema). Getting this wrong reports a flat zero rather
than an error, which is why the warehouses used come back in ``meta``.

THE BLOWING LINE IS A SEPARATE STAGE, NOT MORE CONSUMPTION
----------------------------------------------------------
BH-SDL 'BHAKARPUR SIDEL' is the blow-moulding line. In August it issued
955,734 units and every one was a PREFORM (PM0000852 656,753; PM0000594
268,428; PM0000817 30,553). Those preforms BECOME the PET bottles that BH-PC
then consumes, so adding BH-SDL to the consumption scope would count the same
packaging twice -- once as a preform, again as a bottle. It is reported as its
own upstream stage instead, and left out of every total.

BOM EXPLOSION
-------------
``OITT.Qauntity`` (SAP's own typo) is the batch the recipe is written for and
is frequently not 1, so component-per-unit is
``ITT1.Quantity / OITT.Qauntity``. ``ITT1.Type`` must be 4 -- an inventory
item -- because 290 is a resource, a conversion cost that lives in ORSC and
not in OITM. Both traps are documented at length in
``planning_purchase/hana_reader.py``; this module obeys the same rules.

Only ``TreeType = 'P'`` (production) BOMs are exploded. The 241
``TreeType = 'S'`` sales BOMs -- combo and gift packs -- never appear as
invoice lines, because SAP explodes them into their FINISHED children on the
document itself, so one level of explosion is complete rather than
approximate. Verified on August 2026 Oil: all 97 finished items invoiced have
a production BOM, covering 100% of the invoiced quantity.

WHAT COUNTS AS DISPATCH
-----------------------
A/R invoices (``OINV``/``INV1``), net of A/R credit notes (``ORIN``/``RIN1``),
on finished items. Delivery notes are not used: ``ODLN``/``DLN1`` carried 12
lines in the whole of August 2026 and not one was a finished good, because the
plant invoices directly.

``INV1.Quantity`` is in PIECES -- single bottles, ``InvntryUom`` PCS,
``NumInSale`` 1. The '20 PCS' in an item name is the carton configuration only
(correction C-0001); multiplying by it inflates everything about twentyfold.

DAYS OF COVER, AND WHERE THE STOCK ACTUALLY IS
----------------------------------------------
Cover is stock on hand divided by the rate the line burned it over the period
asked for. Two decisions carry it.

**The rate is per WORKING day, not per calendar day.** This factory runs
Monday to Saturday (``PLANNING_NON_WORKING_WEEKDAYS``, Sunday only), so August
2026 was 26 working days and not 31. Dividing by calendar days understates the
burn rate by 19% and overstates every cover figure by the same, which is the
expensive direction to be wrong in -- it says there is a fortnight of caps left
when there are eleven days. Cover is therefore quoted in working days too.

**The stock scope deliberately differs from ``planning_purchase``.** That
module scopes packaging to BH-PS, BH-PC, BH-PM, and it is right to for its own
question -- what a NEW plan may spend. It is the wrong list here. Verified on
the live Oil company:

    BH-PM   Packaging Materials 1st Floor    4,602,149    Rs 1.33 Cr
    BH-BS   Bhakharpur Basement              2,868,027    Rs 1.19 Cr   <- missing
    BH-PC   Production Consumption           3,097,796    Rs 0.80 Cr
    GP-PM   Gupta Godown Packaging Material    442,006    Rs 0.76 Cr   <- missing
    BH-PS   Bhakharpur Preshit                       0             -

BH-PS holds nothing at all, while BH-BS moved 2,955,062 units in and 2,657,979
out in August across 445 lines -- an active store, not a dumping ground. Using
the producibility list would hide Rs 1.95 crore of real packaging and report
every item as closer to running out than it is.

What stays OUT is as deliberate: BH-WST (wastage, 560,759 units of scrap),
BH-NM and GP-NM (non-moving), BH-JW (job work) and every depot. None of it is
stock the line can draw on tomorrow. BH-SDL is out too -- its 128,570 units are
the blowing line's preform buffer, which belongs to the upstream stage.

All of it is settings, and the warehouses actually used come back in ``meta``
so the figure on screen can always be audited against the list behind it.

INTERCOMPANY IS INCLUDED BY DEFAULT, AND THAT IS DELIBERATE
-----------------------------------------------------------
Correction C-0005 names the group CardCodes that must be excluded from sales
and turnover so the group does not count one sale twice. This dashboard does
not measure revenue -- it measures packing material physically leaving the
factory -- and an intercompany truck leaves just as loaded as any other. In
August 2026 Oil invoiced 1,999,070 pcs of finished goods, of which 1,329,822
(66%) went to group companies: JIVO MART PVT LTD 1,289,822 and JIVO WELLNESS
PVT LTD - PB 40,000. Excluding them would report 669k pcs dispatched against
1,754k produced and imply a million pieces of finished goods piling up that
are not there.

So intercompany counts by default, ``include_intercompany=false`` takes it out
for anyone who wants the third-party-only view, and the summary always carries
both figures so neither reading can be quoted as the other.
"""

from typing import Dict, List, Sequence

from django.conf import settings

# ---------------------------------------------------------------------------
# Item groups
# ---------------------------------------------------------------------------

# OITB.ItmsGrpCod for packing material and for finished goods. Same codes in
# all three company schemas.
PM_ITEM_GROUP = 105
FG_ITEM_GROUP = 102

# The name code 105 is expected to carry. The reader compares and reports.
PM_ITEM_GROUP_NAME = "PACKAGING MATERIAL"

# ---------------------------------------------------------------------------
# OINM movement types
# ---------------------------------------------------------------------------

TRANS_TYPE_GOODS_RECEIPT = 59  # receipt from production (FG made, PM blown)
TRANS_TYPE_GOODS_ISSUE = 60  # issue to production -- actual consumption
TRANS_TYPE_STOCK_TRANSFER = 67  # store-to-line transfer, and scrap to wastage

# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

BOM_TREE_TYPE_PRODUCTION = "P"
BOM_LINE_TYPE_ITEM = 4  # 290 is a resource (a conversion cost), never a material

# ---------------------------------------------------------------------------
# Warehouse scope, per company, all overridable from settings
# ---------------------------------------------------------------------------

DEFAULT_FG_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-PF",),
    "JIVO_BEVERAGES": ("BH-PF",),
    "JIVO_MART": ("DL-MP",),
}

DEFAULT_CONSUMPTION_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-PC",),
    "JIVO_BEVERAGES": ("BH-PP",),
    "JIVO_MART": ("BH-PC",),
}

DEFAULT_WASTAGE_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-WST",),
    "JIVO_BEVERAGES": ("BH-WST",),
    "JIVO_MART": ("BH-WST",),
}

# The blow-moulding line: its own stage, never added to consumption.
DEFAULT_UPSTREAM_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-SDL",),
    "JIVO_BEVERAGES": (),
    "JIVO_MART": (),
}

# Where packaging the line could draw on tomorrow actually sits -- see the
# module docstring for why this is not planning_purchase's list. Wastage,
# non-moving, job-work, the blowing line's own buffer and every depot are out.
DEFAULT_STOCK_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-PM", "BH-BS", "BH-PC", "GP-PM", "BH-PS"),
    "JIVO_BEVERAGES": ("BH-PM", "BH-PP", "GP-PM"),
    "JIVO_MART": ("BH-PM", "GP-PM"),
}

# ---------------------------------------------------------------------------
# Intercompany customers (correction C-0005), per company
# ---------------------------------------------------------------------------

INTERCOMPANY_CARD_CODES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": (
        "CUSTA000001",
        "CUSTA000002",
        "CUSTA000003",
        "CUSTA000004",
        "CUSTA000606",
        "CUSTA000827",
        "CUSTA000906",
        "CUSTA001099",
        "CUSTA001113",
    ),
    "JIVO_MART": (
        "CUSTA000001",
        "CUSTA000827",
        "CUSTA000874",
        "CUSTA000875",
        "CUSTA000876",
        "CUSTA000877",
        "CUSTA000878",
        "CUSTA000926",
    ),
    "JIVO_BEVERAGES": (
        "CUSTA000001",
        "CUSTA000002",
        "CUSTA000003",
        "CUSTA000004",
        "CUSTA000606",
        "CUSTA000827",
    ),
}

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Where the quantities come from. The RECIPE and the STOCK always come from
# SAP whatever this says -- see app_reader for why.
SOURCE_SAP = "sap"
SOURCE_APP = "app"
SOURCES = (SOURCE_SAP, SOURCE_APP)

# What the consumption column actually measures, per source. SAP records the
# goods issue; the app records only the approved BOM, because its issued_qty
# is written by nothing. The API reports this so no screen can present the one
# as the other.
CONSUMPTION_BASIS = {
    SOURCE_SAP: "issued",
    SOURCE_APP: "approved",
}

DEFAULT_TOP_N = 10
MAX_TOP_N = 50

# Cover bands, in working days. A buyer's lead time on printed packaging is
# the reason these are not round numbers pulled from nowhere: under a week is
# inside the reprint lead time and is an order to place today; under a
# fortnight is inside the delivery window and is worth watching.
COVER_CRITICAL_DAYS = 7
COVER_LOW_DAYS = 15

# Widest window a single request may ask for. A year of movements across four
# queries is already a heavy HANA read, and nobody reads a top-10 over five
# years.
MAX_RANGE_DAYS = 400


def _clean(values) -> List[str]:
    """Tolerate a hand-edited .env: trim padding, drop blanks."""
    return [str(code).strip() for code in (values or []) if str(code).strip()]


def _scoped(
    setting_name: str,
    defaults: Dict[str, Sequence[str]],
    company_code: str,
) -> List[str]:
    """A warehouse list from settings if set, else the per-company default.

    The setting is a flat list and replaces the per-company map entirely -- a
    deployment that overrides it means "these warehouses, whatever the
    company", which is what a single-company install wants.
    """
    override = getattr(settings, setting_name, None)
    if override:
        return _clean(override)
    return _clean(defaults.get(company_code, ()))


def fg_warehouses(company_code: str) -> List[str]:
    return _scoped("PM_DEMAND_FG_WAREHOUSES", DEFAULT_FG_WAREHOUSES, company_code)


def consumption_warehouses(company_code: str) -> List[str]:
    return _scoped(
        "PM_DEMAND_CONSUMPTION_WAREHOUSES",
        DEFAULT_CONSUMPTION_WAREHOUSES,
        company_code,
    )


def wastage_warehouses(company_code: str) -> List[str]:
    return _scoped(
        "PM_DEMAND_WASTAGE_WAREHOUSES", DEFAULT_WASTAGE_WAREHOUSES, company_code
    )


def upstream_warehouses(company_code: str) -> List[str]:
    return _scoped(
        "PM_DEMAND_UPSTREAM_WAREHOUSES", DEFAULT_UPSTREAM_WAREHOUSES, company_code
    )


def stock_warehouses(company_code: str) -> List[str]:
    return _scoped(
        "PM_DEMAND_STOCK_WAREHOUSES", DEFAULT_STOCK_WAREHOUSES, company_code
    )


def non_working_weekdays() -> Sequence[int]:
    """Python weekday numbers the factory does not run, Monday 0 .. Sunday 6.

    Read from the same setting the planning module uses, so a change of shift
    pattern moves both screens at once and neither can quietly disagree with
    the other about how long a month is.
    """
    configured = getattr(settings, "PLANNING_NON_WORKING_WEEKDAYS", (6,))
    try:
        days = {int(day) for day in configured or ()}
    except (TypeError, ValueError):
        return (6,)
    # A configuration that closes the factory all week would make every rate
    # infinite; fall back rather than divide by nothing.
    return tuple(sorted(days)) if len(days) < 7 else (6,)


def intercompany_card_codes(company_code: str) -> List[str]:
    override = getattr(settings, "PM_DEMAND_INTERCOMPANY_CARD_CODES", None)
    if override:
        return _clean(override)
    return _clean(INTERCOMPANY_CARD_CODES.get(company_code, ()))

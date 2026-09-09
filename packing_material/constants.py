"""
packing_material/constants.py

Every SAP fact this board rests on, in one place, with the evidence. Verified
against the live JIVO_OIL_HANADB schema on 9 September 2026; the figures below
are what the queries in ``hana_reader`` actually returned.

WHAT COUNTS AS PACKING MATERIAL
-------------------------------
``OITM.ItmsGrpCod`` = 105, whose ``OITB.ItmsGrpNam`` reads 'PACKAGING MATERIAL'
(finished goods are 102, 'FINISHED'). The reader checks that the code and the
name still agree and reports what it found, rather than trusting 105 blind: a
renamed or renumbered group would otherwise make every figure on the board
quietly wrong instead of visibly wrong. An item-code prefix is only a naming
convention and is never matched on.

THE THREE STOCK WAREHOUSES ARE THE ONES THE FACTORY NAMED
---------------------------------------------------------
The four stock cards count BH-PC, BH-BS and BH-PM, and total those three.
Live packing-material stock, all warehouses, ranked by value:

    BH-PP   Production Process 1st Floor   4,983,774   Rs 1.64 Cr   inactive
    BH-PM   Packaging Materials 1st Floor  5,912,210   Rs 1.47 Cr
    BH-BS   Bhakharpur Basement            2,812,191   Rs 1.17 Cr
    BH-PC   Production Consumption         3,050,403   Rs 0.81 Cr
    BH-WST  Bhakharpur WASTAGE               560,759   Rs 0.31 Cr
    GP-NM   Gupta Non-Moving               1,555,886   Rs 0.24 Cr
    BH-NM   Bhakharpur Non-Moving          1,081,977   Rs 0.12 Cr
    GP-PM   Gupta Godown Packaging Mat.      442,006   Rs 0.08 Cr

Two of those omissions are worth stating plainly, because they are the ones
somebody will ask about.

**BH-PP holds more packaging by value than any of the three cards** and is out
because SAP has it flagged ``Inactive = 'Y'`` in Oil's schema -- it is
Beverages' production floor, not Oil's, and its balance sits frozen. It IS the
consumption warehouse for the Beverages company, where it is counted.

**GP-PM is a third-party godown**, Rs 0.08 Cr, and is out because the three
cards are the stores the factory itself works out of.

An inactive warehouse is reported with an ``inactive`` flag rather than
silently dropped. The warehouse list is explicit and per company, so a
warehouse somebody asked for must show what it holds -- and say that SAP has
it decommissioned -- instead of returning a zero that reads as "empty".

WHAT COUNTS AS CONSUMPTION
--------------------------
``OINM.TransType`` = 60 (goods issue) with ``OutQty`` out of BH-PC, the
production-consumption store. This is the load-bearing choice of the whole
board, and it is NOT what ``production_execution.reconciliation_reader`` does
-- that reads TransType 67 *into* BH-PC, which is the store-to-line transfer.
The two disagree badly wherever the factory blows its own bottles. August
2026, Oil:

    PM0000121  PET BOTTLE 1 LTR 52 GMS POMACE
        transferred into BH-PC (67 InQty)             2
        blown in-house into BH-PC (59 InQty)    662,769
        issued out of BH-PC (60 OutQty)         603,505   <- what was used

Reading the transfer would have reported 2 bottles against 603,505 really
consumed. TransType 60 out of BH-PC is the only figure that answers "what did
the line use", and it is the only one this board reads: BH-PM and BH-BS are
stores that FEED BH-PC, so counting their issues too would count the same
carton once on its way to the floor and again on its way into a case.

Live sanity check on the query: 201 packing items issued out of BH-PC in
August 2026, 124 in the first eight days of September.

BOM EXPLOSION, FOR THE DISPATCH SECTION
---------------------------------------
``OITT.Qauntity`` (SAP's own typo) is the batch the recipe is written for and
is frequently not 1, so component-per-unit is
``ITT1.Quantity / OITT.Qauntity``. ``ITT1.Type`` must be 4 -- an inventory
item -- because 290 is a resource, a conversion cost that lives in ORSC and
not in OITM. Both traps are documented at length in
``planning_purchase/hana_reader.py``; this module obeys the same rules. 2,003
production-BOM lines on Oil have a packing-material component.

Only ``TreeType = 'P'`` (production) BOMs are exploded. Sales BOMs -- combo
and gift packs -- never appear as invoice lines, because SAP explodes them
into their FINISHED children on the document itself, so one level of
explosion is complete rather than approximate. An item invoiced with no
production BOM cannot be exploded at all; how much of the period that applies
to comes back as ``coverage`` rather than being left to look like zero
packaging.

WHAT COUNTS AS DISPATCH -- TWO ANSWERS, BOTH TRUE
-------------------------------------------------
``source=sap`` reads A/R invoices (``OINV``/``INV1``) net of A/R credit notes
(``ORIN``/``RIN1``) on finished items. Delivery notes are not used: the plant
invoices directly. August 2026 Oil: 603 invoices, 1,231 finished-goods lines,
1,999,070 pieces.

``source=app`` reads FactoryFlow's own gate-out register -- the bills that
physically went out through docking. Only ``status = DISPATCHED`` counts, and
the date is ``gate_out_date``, the day the truck left. August 2026 Oil: 125
gate-outs carrying 396 bills and 962 item lines.

The two are NOT the same set, which is exactly why the toggle is worth having:
396 of the 603 invoices SAP raised in August (66%) have a FactoryFlow docking
behind them. The rest were invoiced without passing one. Neither figure is
wrong; they answer different questions, and the response labels which one it
just answered.

``INV1.Quantity`` and ``SalesDispatchGateOutItem.quantity`` are both in
PIECES, verified line for line against one another on bill 626080610 (140,
3900, 130, 500 and 400 pieces, agreeing exactly). The '20 PCS' in an item name
is the carton configuration only (correction C-0001); multiplying by it
inflates everything about twentyfold.

PACKING MATERIAL INVOICED AS ITSELF
-----------------------------------
Some bills carry packing material as a line in its own right -- 18 of the
distinct items on August's gate-out lines are group 105, alongside 81 finished
goods. That packaging left the factory too, but it did not leave *inside* a
finished good, so it is not exploded through a BOM and not added to the top
list. It is reported as ``coverage.direct_pm_qty`` so the figure is visible
rather than missing.

INTERCOMPANY IS INCLUDED, AND THAT IS DELIBERATE
------------------------------------------------
Correction C-0005 names the group CardCodes that must be excluded from sales
and turnover so the group does not count one sale twice. This board does not
measure revenue -- it measures packing material physically leaving the factory
-- and an intercompany truck leaves just as loaded as any other. In August
2026 Oil invoiced 1,999,070 pieces of finished goods, of which 1,329,822 (66%)
went to group companies. Excluding them would report a third of the real
packaging and imply a million pieces of finished goods piling up that are not
there. The split comes back in the summary so neither reading can be quoted as
the other.
"""

from typing import Dict, List, Sequence

from django.conf import settings

# ---------------------------------------------------------------------------
# Item groups
# ---------------------------------------------------------------------------

PM_ITEM_GROUP = 105
FG_ITEM_GROUP = 102

# The name code 105 is expected to carry. The reader compares and reports.
PM_ITEM_GROUP_NAME = "PACKAGING MATERIAL"

# ---------------------------------------------------------------------------
# OINM movement types
# ---------------------------------------------------------------------------

TRANS_TYPE_GOODS_ISSUE = 60  # issue to production -- actual consumption

# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

BOM_TREE_TYPE_PRODUCTION = "P"
BOM_LINE_TYPE_ITEM = 4  # 290 is a resource (a conversion cost), never a material

# ---------------------------------------------------------------------------
# Warehouse scope, per company, all overridable from settings
# ---------------------------------------------------------------------------

# The stock cards, IN CARD ORDER: the consumption store first, then the stores
# that feed it. Only the Oil list is verified against live data -- the other
# two follow the same shape using each company's own production floor, and a
# deployment that disagrees overrides the whole map from settings.
DEFAULT_STOCK_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-PC", "BH-BS", "BH-PM"),
    "JIVO_BEVERAGES": ("BH-PP", "BH-PM"),
    "JIVO_MART": ("BH-PM",),
}

# Where the line issues from. One warehouse per company by design -- see the
# module docstring for why adding the feeding stores double-counts.
DEFAULT_CONSUMPTION_WAREHOUSES: Dict[str, Sequence[str]] = {
    "JIVO_OIL": ("BH-PC",),
    "JIVO_BEVERAGES": ("BH-PP",),
    "JIVO_MART": ("BH-PC",),
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
# Where the dispatch section reads from
# ---------------------------------------------------------------------------

SOURCE_SAP = "sap"
SOURCE_APP = "app"
SOURCES = (SOURCE_SAP, SOURCE_APP)

# What the dispatch column actually measures, per source, so no screen can
# present the one as the other.
DISPATCH_BASIS = {
    SOURCE_SAP: "invoiced",
    SOURCE_APP: "gated-out",
}

# ---------------------------------------------------------------------------
# Ranking and limits
# ---------------------------------------------------------------------------

DEFAULT_TOP_N = 10
MAX_TOP_N = 50

# Both sections rank on QUANTITY, which is the factory's own way of counting
# packaging. Value is carried on every row alongside it, because a top ten by
# pieces is led by caps and labels while a top ten by rupees is led by
# bottles, and the two readings answer different questions.
RANKED_BY = "qty"

# Widest window a single request may ask for. A year of movements is already a
# heavy HANA read, and nobody reads a top-10 over five years.
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


def stock_warehouses(company_code: str) -> List[str]:
    return _scoped(
        "PACKING_MATERIAL_STOCK_WAREHOUSES", DEFAULT_STOCK_WAREHOUSES, company_code
    )


def consumption_warehouses(company_code: str) -> List[str]:
    return _scoped(
        "PACKING_MATERIAL_CONSUMPTION_WAREHOUSES",
        DEFAULT_CONSUMPTION_WAREHOUSES,
        company_code,
    )


def intercompany_card_codes(company_code: str) -> List[str]:
    override = getattr(settings, "PACKING_MATERIAL_INTERCOMPANY_CARD_CODES", None)
    if override:
        return _clean(override)
    return _clean(INTERCOMPANY_CARD_CODES.get(company_code, ()))


# ===========================================================================
# The requirement board (plan vs. what is left to buy)
# ===========================================================================
#
# A second question on the same material, asked by the buyer rather than by
# management: the month's production plan exploded through its bills of
# material, netted against what the floor has already taken and what the
# stores still hold, and finally against what is already on order.
#
# The nine columns, and the arithmetic between them:
#
#     Planning       plan qty x BOM qty per unit, summed per component
#     Issue (PC)     receipts into the consumption store, 1st of month -> today
#     Rest Planning  Planning - Issue (PC)
#     On hand        stock in the FEEDING stores -- not the consumption store
#     Req            On hand - Rest Planning        (negative = short)
#     PO             quantity on open purchase orders
#     REQ after PO   Req + PO                       (negative = still short)
#
# VERIFIED AGAINST THE PLANNER'S OWN SHEET
# ----------------------------------------
# Reproduced column for column against the spreadsheet the packaging buyer
# keeps by hand, on the September 2026 plan (OFCT AbsID 44) read on
# 9 September 2026. Eleven rows checked; `Issue (PC)` and `On hand` matched
# EXACTLY on all eleven, and `Planning` on seven:
#
#     PM0000235  CAPS 1 LTR WHITE AND YELLOW SMALL PLAIN
#         Planning 1,102,500  Issue 155,000  Rest 947,500
#         On hand    405,000  Req -542,500          <- sheet, and this module
#     PM0000468  CAPS 5 LTR GREEN
#         Planning    56,867  Issue  13,552  Rest  43,315
#         On hand     45,583  Req    2,268
#
# The four `Planning` rows that differ do so in BOTH directions (PM0000079
# -17,500, PM0000085 +25,000) rather than by a constant factor, which is plan
# drift: the sheet was built on 27 August against the draft, and planners have
# edited OFCT since. A method error would bias one way. This board reads the
# plan live for exactly that reason.
#
# WHY `On hand` EXCLUDES THE CONSUMPTION STORE
# --------------------------------------------
# `Issue (PC)` is already counted as plan fulfilled -- material that reached
# the floor is treated as though the plan it was drawn for has been produced.
# Counting the same store's balance again as "available" would credit it
# twice: once as production already done, once as stock still to be used. So
# `On hand` is the stores that FEED the floor, derived as the stock warehouses
# minus the consumption warehouse rather than listed separately, so the two
# lists cannot drift apart. Oil: BH-BS and BH-PM.
#
# WHY `Issue (PC)` COUNTS EVERY RECEIPT AND NOT JUST THE TRANSFER
# ---------------------------------------------------------------
# Material arrives in the consumption store two ways, and both mean the plan
# was worked against:
#
#     TransType 67 InQty   transferred in from BH-BS / BH-PM
#     TransType 59 InQty   blown or made in-house straight onto the floor
#
# September 2026, receipts into BH-PC of packing material: 2,171,718 by
# transfer across 81 items, and 314,175 produced in-house across just 3 --
# PET BOTTLE 1 LTR 40 GMS (187,085), PET BOTTLE 1 LTR 52 GMS POMACE (126,982)
# and one more. Counting only the transfer would report those three bottles as
# barely touched, leave `Rest Planning` at nearly the full month, and raise a
# six-figure shortage on an item the factory does not buy at all -- it buys the
# preform. Both are summed, and the split is returned on every row
# (`issued_transfer_qty` / `issued_produced_qty`) so any row can be read either
# way without re-querying.
#
# This is the mirror of the trap recorded at the top of this file: reading the
# TRANSFER as consumption understated a PET bottle 2 against 603,505. There,
# 60-OutQty was the answer; here, receipts are, because the question is not
# "what did the line use" but "what has the plan already consumed".
#
# WHAT IS DELIBERATELY NOT NETTED OFF
# -----------------------------------
# `Req` does NOT subtract `OITW.IsCommited`. The commitment on a packing
# material is overwhelmingly the plan's own production orders, so netting it
# would subtract the same demand twice -- once as `Planning`, once as
# committed. `planning_purchase` DOES net it, correctly, because it starts
# from a different figure. The two boards therefore disagree by design and
# each says which it is.
#
# NEITHER FIGURE IS FLOORED AT ZERO
# ---------------------------------
# `Rest Planning` goes negative when the floor drew more than the plan called
# for -- 3 of 196 components in September -- and `Req` and `REQ after PO` go
# negative for the ordinary case of a shortage, which is the whole point of
# the board. Flooring `Rest Planning` would hide over-issue; flooring `Req`
# would hide the shortage. Over-issued rows are flagged `over_issued` instead,
# and the row keeps the arithmetic the buyer's sheet does.

# OINM movement types that put material ONTO the floor.
TRANS_TYPE_TRANSFER_IN = 67  # stock transfer between warehouses
TRANS_TYPE_PRODUCTION_RECEIPT = 59  # made in-house, straight into the store

# OFCT/FCT1 is where this factory authors its monthly production plan -- every
# header in Oil is named "OIL Monthly Production Planning for the <Month>
# <Year>". OWOR (production orders) is NOT the plan and is not read; see
# `planning_purchase/hana_reader.py`, which established this.
PLAN_MONTHLY_FORM_VIEW = "M"

# OPOR/POR1 status for a purchase order line still to be delivered.
PO_STATUS_OPEN = "O"

# How many plan headers the picker offers. Three years of months.
PLAN_LIST_LIMIT = 36
MAX_PLAN_LIST_LIMIT = 120

# Components whose name/code the meta block names before it stops counting.
MAX_LISTED_ITEMS = 25


def supply_warehouses(company_code: str) -> List[str]:
    """The stores that FEED the floor -- what `On hand` counts.

    Derived as the stock warehouses minus the consumption warehouse rather
    than kept as a third list, so it cannot drift out of step with the two it
    is defined from. Oil: (BH-PC, BH-BS, BH-PM) - (BH-PC) = BH-BS, BH-PM.

    A deployment that needs its own answer overrides
    ``PACKING_MATERIAL_SUPPLY_WAREHOUSES`` and the derivation is skipped.
    """
    override = getattr(settings, "PACKING_MATERIAL_SUPPLY_WAREHOUSES", None)
    if override:
        return _clean(override)

    consumed_in = set(consumption_warehouses(company_code))
    return [code for code in stock_warehouses(company_code) if code not in consumed_in]

# How many driving finished goods a component row carries as evidence. A cap
# on the payload only: `planning_qty` is always the sum of every driver, and
# `driver_count` says how many there are, so a truncated list can never be
# read as the whole of it.
MAX_LISTED_DRIVERS = 8

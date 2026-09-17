"""
plant_board/constants.py

Every figure and code the plant control board rests on, in one place, with the
decision behind it.

WHY THIS APP HAS NO MODELS
--------------------------
It composes existing reports and adds three SAP reads. It owns no data, so it
declares no model, ships no migration and creates no permission row — which
means it can be deployed against the live database with nothing to migrate.
See ``permissions.py`` for how it is gated without a right of its own.

THE BAND DEFINITIONS
--------------------
Twelve metric definitions were settled with the business on 10 September 2026.
They are not derivable from the code and several of them overturn what the code
assumed, so each is recorded beside the constant that implements it.

* **Tons = litres / 1000.** Fixed by the business. That is a density of 1.0
  where edible oil is about 0.91, so a tonnage figure reads roughly 10% above a
  weighbridge. Accepted knowingly, and applied to OIL / finished goods only.
* **Packing material is never shown in tons.** ``OITM.U_IsLitre`` is 'N' on all
  875 packaging items, so litres — and therefore tons — do not exist for them.
  The Purchase and Store bands read in pieces.
* **"Purchased" is two figures, not one:** what is on order (open PO) and what
  has arrived (GRPO). They answer different questions and are never blended.
* **Non-moving has no age floor and no percentage.** A share computed with no
  floor reads near 100% every day, because almost every SKU has at least one
  idle day. The tile is a count and a value, oldest first.
* **"Avg produced" is month-to-date output divided by the days that actually
  produced** — days with at least one production receipt — not calendar days
  and not working days.
* **The Production band's "Cost" labels mean CASES.** The run-costing engine is
  deliberately not involved in this band.
* **"OIL waste" is yield loss**, not a waste-log row. See
  ``hana_reader.oil_yield`` for the basis and its one assumption.
* **Age buckets are aged from when stock last LEFT**, never from any movement.
  BH-PF is a floor goods are produced *into*, so an inbound receipt must not
  reset the clock on a batch that has never shipped.
* **The window for Purchase and Production is the SAP plan month**, plan-to-date
  against plan total — not the calendar month.

THE COMPANY IS OIL, AND THE FILTER GOES ON THE SOURCE
-----------------------------------------------------
The board is Jivo Oil, and the Shifting band filters BST on the **source**
company and the **source** warehouse. Both halves of that matter. Most of what
leaves BH-PF is an INVOICE transfer — a cross-company SALE to Mart — so a
filter on the destination would drop the largest column on the band. And BH-BT
runs its own transfers out to Mart, so a filter on the company alone would
count a second floor's day as this floor's.
"""

from packing_material.constants import PM_ITEM_GROUP_NAME

# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------

#: The production-finished floor. What the Production band reports on.
PRODUCTION_FLOOR = "BH-PF"

#: The floor the packaging stores stand on, in square feet.
#:
#: Given by the business on 12 September 2026 as a three-row table. Held here
#: rather than on the settings page because a building's area is a fact about
#: the site, not a figure anybody re-types monthly -- and unlike the rated
#: TONNAGE capacity beside it, nothing about it changes with what is stored.
#:
#: THE FIRST BLOCK IS SHARED AND CANNOT BE SPLIT. 16,900 sq ft covers BH-PM,
#: BH-NM and BH-BS together as one physical space; there is no per-warehouse
#: breakdown of it and inventing one would put a number on the board that
#: nobody measured. So the board totals the blocks and names them, rather than
#: reporting an area per warehouse it cannot stand behind.
#:
#: "FR" WAS IN THE BUSINESS'S TABLE AND IS EXCLUDED, ON THEIR INSTRUCTION.
#: It is also not a warehouse SAP knows: nothing matching it exists in ``OWHS``
#: for Oil or Mart, active or inactive. Excluding it does not reduce the
#: 16,900 -- that block is measured as a whole, so what is dropped is the
#: warehouse, not a share of the floor.
PACKAGING_FLOOR_BLOCKS = [
    {
        "key": "shared",
        "label": "BH-PM, BH-NM, BH-BS",
        "warehouses": ["BH-PM", "BH-NM", "BH-BS"],
        "sqft": 16900,
    },
    {"key": "pm", "label": "BH-PM", "warehouses": ["BH-PM"], "sqft": 15000},
    {"key": "pc", "label": "BH-PC", "warehouses": ["BH-PC"], "sqft": 7000},
]

#: 38,900 sq ft across the four stores.
PACKAGING_FLOOR_SQFT = sum(block["sqft"] for block in PACKAGING_FLOOR_BLOCKS)

#: The floor a single pallet stands on, as the factory measured it.
#:
#: A DEFAULT, NOT A PLACEHOLDER. The settings page can override it per company,
#: because a site with different pallets has a different number -- but this is a
#: measurement somebody took, so the board uses it from the first refresh rather
#: than reporting that it cannot say how full the stores are until a form is
#: filled in. An unconfigured board should be right, not silent.
DEFAULT_SQFT_PER_PALLET = 15.0


# ---------------------------------------------------------------------------
# Who works in each band
# ---------------------------------------------------------------------------

#: The plant's departments, and where each one belongs on the board.
#:
#: THE MAPPING IS A STATEMENT ABOUT THE FACTORY, SO IT LIVES IN CODE. The head
#: count and the wage bill are typed on the settings page and change monthly;
#: which band a department reports under, and whether its people are staff or
#: hired labour, does not. Keeping the two apart means an operator can never
#: accidentally move a department to another band by editing a number.
#:
#: ``kind`` fills the two halves of a band's workforce strip. The business's own
#: "Company" and "Outside" split is exactly that distinction: company is on the
#: payroll, outside is hired in. Several departments can share a half — the
#: Store band's staff are the blowing line's and the PM warehouse's together —
#: and the strip sums them.
#:
#: Purchase has no department. That is deliberate rather than missing: buying
#: packing material is desk work, and the people who receive it are the PM
#: warehouse, which the business placed under Store.
WORKFORCE_DEPARTMENTS = [
    {
        "key": "oil_production_company",
        "label": "Oil Production Company",
        "band": "production",
        "kind": "employee",
    },
    {
        "key": "oil_production_outside",
        "label": "Oil Production Outside",
        "band": "production",
        "kind": "labour",
    },
    {
        "key": "bottle_blowing_company",
        "label": "Bottle Blowing Company",
        "band": "store",
        "kind": "employee",
    },
    {
        "key": "bottle_blowing_outside",
        "label": "Bottle Blowing Outside",
        "band": "store",
        "kind": "labour",
    },
    {
        "key": "pm_warehouse",
        "label": "Pm Warehouse",
        "band": "store",
        "kind": "employee",
    },
    {
        "key": "fg_shifting",
        "label": "Fg Shifting",
        "band": "shifting",
        "kind": "employee",
    },
]

#: The bands a workforce strip can appear under, in board order.
WORKFORCE_BANDS = ["purchase", "store", "production", "shifting"]


#: Where finished stock goes when it leaves the production floor.
#:
#: THE ORDER IS FIXED AND IS NOT A RANKING. Both Shifting tiles read down the
#: same three rows every minute of the day, so a route must not change place
#: when it happens to be the biggest — a wall figure that moves is a figure
#: nobody trusts. A route outside this list is not dropped; it lands in
#: ``SHIFTING_ELSEWHERE`` and the tile says so.
SHIFTING_TO_BOTTLING = "BH-BT"
#: The old Gupta code, and NOT a standing row any more.
#:
#: The Gupta godown holds Mart's stock, so a load going there is the same event
#: as the dispatch to Mart -- but SAP recorded it as an Oil internal transfer
#: (``source_type`` STOCK_TRANSFER), which put it on this board as a third
#: godown alongside a Dispatch row that meant the same thing. The business now
#: books Mart's Gupta stock into Mart's own warehouse ``GP-FGM``, where it
#: reaches this board as a dispatch like any other cross-company sale.
#:
#: Kept as a constant, and kept in the name map, so that a load still posted
#: against the old code is named rather than anonymous: it no longer has a row
#: of its own, it folds into Elsewhere, and Elsewhere prints the codes it
#: folded. Not deleted outright, because 236,582 pieces moved on this code in
#: the last sixty days and a figure that size must never leave the board
#: without saying where it went.
SHIFTING_TO_GUPTA = "GP-FG"
#: Mart's own Gupta warehouse, which replaces the code above. Deliberately NOT
#: a route: it belongs to Mart's schema, so an Oil BST cannot target it at all
#: -- stock reaching it is a sale, and a sale is already the Dispatch row.
SHIFTING_TO_GUPTA_MART = "GP-FGM"
#: Not a warehouse. A BST sourced from an AR invoice is a cross-company SALE
#: (Oil -> Mart), so its stock left the company rather than moving inside it,
#: and it carries no destination warehouse at all.
SHIFTING_DISPATCH = "DISPATCH"
SHIFTING_ELSEWHERE = "ELSEWHERE"

SHIFTING_ROUTES = [SHIFTING_TO_BOTTLING, SHIFTING_DISPATCH]

#: The declaration tile's rows: the bottling floor, and no dispatch column.
#:
#: The keeper's register CAN hold a dispatch, and one is not thrown away -- it
#: folds into the Elsewhere row, which appears only when it carries something.
#: But it is not given a standing row of its own, because on this register it
#: almost never does: what leaves the floor on a sale is raised as an invoice
#: and reaches the board through BST, where it has a row and a real figure.
#: A standing row that reads 0.0 t every day of the year teaches a wall reader
#: to skip that line, and the day it is not zero he skips it anyway.
SHIFTING_DECLARED_ROUTES = [SHIFTING_TO_BOTTLING]

#: SAP's own warehouse names, so the wall reads a place rather than a code.
SHIFTING_ROUTE_NAMES = {
    SHIFTING_TO_BOTTLING: "Bhakharpur New Basement",
    # No longer a route; kept so a load on the retired code can still be named
    # inside the Elsewhere row.
    SHIFTING_TO_GUPTA: "Gupta Godown Basement",
    SHIFTING_TO_GUPTA_MART: "Gupta Godown (Mart)",
    SHIFTING_DISPATCH: "Sold on to Mart",
    SHIFTING_ELSEWHERE: "Other destinations",
}

#: The packaging stores the SKU count, benchmark and store tiles cover.
#:
#: The business named these. Verified against live stock on 12 September 2026 --
#: every one holds real packaging material:
#:
#:     BH-PM   Packaging Materials 1st Floor   279 SKUs   7,000,314 pcs
#:     BH-PC   Production Consumption          306 SKUs   2,946,172 pcs
#:     BH-BS   Bhakharpur Basement             128 SKUs   2,445,660 pcs
#:     BH-NM   Bhakharpur Non-Moving            49 SKUs   1,081,977 pcs
#:
#: BH-NM is a store like any other here. It holds packaging that has stopped
#: moving, and leaving it out did not make that stock disappear -- it made the
#: board understate what the factory is holding, which is the opposite of what
#: the Against-benchmark tile is for.
#:
#: Two exclusions worth stating, because somebody will ask. BH-PP holds more
#: packaging by value than any of these and is out because SAP has it
#: ``Inactive = 'Y'`` in Oil's schema: it is Beverages' production floor, where
#: it is counted. GP-PM is a third-party godown.
#:
#: THE BUSINESS ALSO NAMED "FR", AND NO SUCH WAREHOUSE EXISTS. Nothing matching
#: it is in ``OWHS`` for Oil or Mart, active or inactive. It is left out rather
#: than guessed at: BH-FG and BH-GR are the near misses and they are different
#: places holding different stock.
STORE_WAREHOUSES = ["BH-PM", "BH-BS", "BH-NM", "BH-PC"]

# ---------------------------------------------------------------------------
# SAP item groups and movement types
# ---------------------------------------------------------------------------

#: ``OITB.ItmsGrpCod`` 102, name 'FINISHED'.
FINISHED_ITEM_GROUP = 102

#: ``OITB.ItmsGrpCod`` 105, name 'PACKAGING MATERIAL'.
PACKAGING_ITEM_GROUP = 105

# ---------------------------------------------------------------------------
# The benchmark tile, which must agree with the Stock Benchmark dashboard
# ---------------------------------------------------------------------------
#
# These three are the Stock Benchmark page's OWN defaults, restated here so the
# two screens cannot report different counts for the same question. A reader who
# sees this tile and then opens that page must find the same number, and the
# only way to promise that is to send the same filter.
#
# Each one narrows the set, and leaving any off inflates the tile:
#
#   * ``BENCHMARK_STATUSES`` drops items with no benchmark set. They cannot be
#     judged against one, so counting them makes a store with no benchmarks
#     configured look like a store in good order.
#   * ``BENCHMARK_MOVEMENT`` drops slow movers. SAP's own status SQL excludes
#     them from every health status, so a board that counts them is counting
#     rows the page has already set aside.
#   * ``BENCHMARK_ITEM_GROUP`` is the page's default material type. Matched on
#     the group NAME, because that is what the filter sends and the backend
#     compares with an exact (upper-cased) equality -- so the name has to be
#     SAP's, letter for letter. It is imported from ``packing_material`` rather
#     than typed here: that module verified it against the live company, and
#     the frontend's own fallback string ("Packing Material") is NOT it. SAP
#     says PACKAGING MATERIAL, and a near-miss does not fail loudly, it matches
#     nothing and the tile reads zero.
#
# The count is read as healthy + low + critical, which is what the page's own
# "Total Items" card shows -- not the unfiltered row count, which still carries
# the slow movers.
BENCHMARK_STATUSES = ["healthy", "low", "critical"]
BENCHMARK_MOVEMENT = ["recent"]
BENCHMARK_ITEM_GROUP = PM_ITEM_GROUP_NAME

# ---------------------------------------------------------------------------
# The non-moving tile, which must agree with the Non-Moving dashboard
# ---------------------------------------------------------------------------
#
# That dashboard's own thresholds, from `non-moving/utils/movementStatus.ts`:
# over 45 idle days is non-moving, 30 to 45 is slow-moving, under 30 is
# recently moved. Its default status filter shows the first two and HIDES the
# third, so a tile that counts every row counts stock the page does not.
#
# It also folds a SKU held in several warehouses onto one line keeping the
# FRESHEST movement of the group, not the oldest — deliberately, so an item
# consumed in one store does not read as dead because a pallet of it sits
# untouched in another. Taking the oldest instead (the obvious choice, and the
# wrong one) both inflates the count and ages every line.
NON_MOVING_SLOW_DAYS = 30
NON_MOVING_DEAD_DAYS = 45

#: Item-group *names* that count as oil for the yield calculation. Matched on
#: the group name because the group is what SAP enforces; an item-code prefix is
#: only a naming convention. Mirrors ``planning_purchase.RAW_TOKENS``.
OIL_GROUP_TOKENS = ("RAW", "OIL")

#: ``OINM.TransType`` 59 — goods receipt from production. What was made.
TRANS_TYPE_PRODUCTION_RECEIPT = 59

#: ``OINM.TransType`` 60 — goods issue. What the line consumed.
TRANS_TYPE_GOODS_ISSUE = 60

# ---------------------------------------------------------------------------
# Board policy
# ---------------------------------------------------------------------------

#: Litres in one ton, as fixed by the business. See the module docstring.
LITRES_PER_TON = 1000

#: The standing-age buckets on the Production band, in days since stock last
#: left. Anything under the first bound is fresh and is not bucketed — BH-PF
#: turns its whole contents over about every 3.3 days, so three days standing is
#: normal and four is the first day worth showing.
AGE_BUCKET_LOWER = 4
AGE_BUCKET_UPPER = 7

#: How often the wall board re-reads. Production moves in minutes, not seconds,
#: and every tile re-runs on each refresh — so this is also the SAP query
#: budget. Sent to the client rather than hardcoded there, so the interval can
#: be slowed down from the server if HANA is struggling.
REFRESH_SECONDS = 60

#: Days of trailing detail a trend carries. A week: enough to see whether
#: today is normal, few enough that each bar is still readable from across a
#: floor. The totals beside a trend always cover the whole month, not this.
TREND_DAYS = 7

#: Rows any one list on the response carries. The board is a wall, not a report:
#: nobody reads row 40 from across a factory floor.
MAX_LISTED_ROWS = 12

#: Tiles this board does not compute, and what each is waiting on. Returned to
#: the client so the wall can label an empty tile with the reason instead of
#: rendering a zero that reads as fact.
PENDING_TILES = {
    # Capacity and the audit date are configured now; what is still missing is
    # the tonnage HELD, because no stock query in this app reads a weight for
    # the packaging stores. So the board can say how much a store is rated for
    # and when it was last counted, but not yet how full it is.
    # The floor area is now known -- 38,900 sq ft across the four stores. What
    # is still missing is the one factor that puts the area and the stock on
    # one scale: nothing in SAP holds a footprint, a pallet count or a stack
    # height for a packaging item.
    "space_percent": "square feet per pallet, to turn stock into floor used",
    "employee_salary": "salary module",
    "labour_salary": "salary module",
}

"""
amounts_board/constants.py

What the Amounts board reads, in one place.

THE CATEGORY IS THE ITEM GROUP, NOT THE GODOWN
----------------------------------------------
Godowns hold mixed stock: on live (2026-10-06) Oil's BH-PC carried Rs 1.37 Cr of
raw material and Rs 0.91 Cr of packing material, and BH-FG held all three. So
RM / PM / FG is decided by the item's SAP group (106 / 105 / 102, the same in
every company schema -- see ``stock_audit.models.ITEM_GROUP_CATEGORY``) and a
godown appears under every category it holds stock of.

Fixed assets (110/112), semi-finished (115), trading items (107) and
consumables are none of the three and are not counted anywhere on the board.
"""

from stock_audit.models import ITEM_GROUP_CATEGORY, ItemCategory

#: The two plant rows, top to bottom.
PLANT_COMPANIES = ("JIVO_OIL", "JIVO_BEVERAGES")

PLANT_LABELS = {
    "JIVO_OIL": "Oil plant",
    "JIVO_BEVERAGES": "Beverage plant",
}

#: The three categories a plant row splits its stock into, in tile order.
CATEGORIES = (ItemCategory.RM, ItemCategory.PM, ItemCategory.FG)

#: Category -> SAP item group, the inverse of stock_audit's map.
CATEGORY_ITEM_GROUP = {category: group for group, category in ITEM_GROUP_CATEGORY.items()}

# --------------------------------------------------------------------------- #
# Non-moving tile
# --------------------------------------------------------------------------- #
# The tile shows the figure the Non-Moving page OPENS on, because clicking it
# lands there and two numbers for one thing would each discredit the other.
# These mirror FactoryFlow's non-moving constants (DEFAULT_NON_MOVING_AGE,
# the default "Packing Material" group, DEFAULT_NON_MOVING_WAREHOUSES); change
# them together.

#: Idle for MORE than this many days (the report's own `>`).
NON_MOVING_AGE_DAYS = 45
NON_MOVING_ITEM_GROUP = CATEGORY_ITEM_GROUP[ItemCategory.PM]
NON_MOVING_WAREHOUSES = ("BH-BS", "BH-NM", "BH-PM", "GP-NM")

# --------------------------------------------------------------------------- #
# Debtors row
# --------------------------------------------------------------------------- #

#: (key, label, company) in tile order. JWPL is Jivo Wellness Pvt Ltd, whose
#: books are the Oil schema (OADM.CompnyName). Beverages is a unit of the same
#: legal entity but keeps its own SAP database, so it is its own tile.
DEBTOR_COMPANIES = (
    ("JWPL", "JWPL", "JIVO_OIL"),
    ("MART", "MART", "JIVO_MART"),
    ("BEVERAGES", "Beverages", "JIVO_BEVERAGES"),
)

#: A customer owing less than this does not set the "oldest debt" date. On live
#: the oldest FIFO dates were otherwise rounding residues: Rs 7, Rs 28, Rs 213
#: left on accounts that were really settled.
OLDEST_DEBT_FLOOR = 1000

#: How often the screen re-reads. Stock values and ledgers move by the hour, not
#: the minute, and every read touches three schemas.
AMOUNTS_BOARD_REFRESH_SECONDS = 300

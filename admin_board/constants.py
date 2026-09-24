"""
admin_board/constants.py

Every code, warehouse and rated figure the admin control board rests on, with
the decision behind it.

WHY THIS APP HAS NO MODELS
--------------------------
It composes existing services and adds three SAP reads. It owns no data, so it
declares no model, ships no migration and creates no permission row — it can be
deployed against the live database with nothing to migrate. Capacity is read
from ``stock_dashboard.WarehouseBoardSettings``, which already exists and is
already edited by the Logistics and Plant boards' settings pages.

THE METRIC DEFINITIONS
----------------------
Settled with the business on 15 September 2026. None is derivable from the
code, and two of them overturn what a reader would assume from the labels:

* **"Total dispatch (Oil | Mart)" is a SUM, not a comparison.** The two SAP
  companies are added into one figure. Intercompany CardCodes are excluded on
  both sides or an internal transfer inflates the total.
* **"Total PM storage — capacity into values" means RUPEES.** The neighbouring
  storage tiles are tonnes; this one is not, because packaging material has no
  litre volume in SAP and therefore no tonnage at all.
* **"Avg" is per ACTIVE day, never per calendar day.** Production divides by
  days that actually produced; dispatch by days that actually dispatched. A
  zero day is excluded from both, so each reads as typical output on a working
  day rather than as an average dragged down by Sundays.
* **Tons = litres / 1000**, the business's fixed rule. Density 1.0 where edible
  oil is about 0.91, so tonnage reads ~10% above a weighbridge. Knowingly
  accepted, and shared with the plant board.

THE GUPTA GODOWN IS ``GP-FGM`` AND ONLY ``GP-FGM``
--------------------------------------------------
Confirmed by the warehouse on 15 September 2026, when asked directly, because
the first reading of the storage sheet got it wrong. ``GP-FG`` — Oil's "Gupta
Godown Basement Finished Godown" — is a DIFFERENT warehouse that appears on no
storage sheet, and it held 213 tonnes of finished goods on the day this was
built. It is deliberately NOT folded into Gupta to make the arithmetic tidy:
doing so would report unrated stock against a rating that does not cover it.
The board names it in an alert instead, so the tonnage is visible without being
counted. See ``UNRATED_FG_WAREHOUSES``.
"""

from company.models import Company

# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------

#: The production-finished floor. Output is counted as it is received here.
PRODUCTION_FLOOR = "BH-PF"

#: The finished-goods stores the FG tile reports, and whose company owns each.
#:
#: Two companies on one tile. BH-BT is Oil's; the Gupta godown is Mart's, and
#: the stock standing in it is Mart's even though the labour working it is
#: Oil's (a genuine split, not a tagging error). A reader asking "how much
#: finished goods is on site" means both, so the tile sums across the company
#: boundary and the rows name which company each came from.
FG_STORES = [
    {"warehouse": "BH-BT", "company": "JIVO_OIL", "label": "BH-BT"},
    {"warehouse": "GP-FGM", "company": "JIVO_MART", "label": "Gupta"},
]

#: Finished goods standing in a warehouse nobody has rated.
#:
#: Real stock in an unrated location. Reported as a figure and an alert, never
#: added to the rated total — see the module docstring for why.
UNRATED_FG_WAREHOUSES = [
    {"warehouse": "GP-FG", "company": "JIVO_OIL", "label": "GP-FG (Oil, Gupta basement)"},
]

#: The packaging stores the PM tile values, mirroring the plant board's blocks.
#:
#: Same four warehouses the plant board's floor-area table covers, so the two
#: boards cannot disagree about what "PM storage" contains. ``GP-PM`` is
#: deliberately absent: it is not on the business's storage sheet.
PM_STORES = ["BH-PM", "BH-BS", "BH-NM", "BH-PC"]

#: The loose-oil tank farm.
OIL_TANK = "BH-LO"

# ---------------------------------------------------------------------------
# SAP movement types
# ---------------------------------------------------------------------------

#: A production receipt. The only inbound that counts as "produced".
#:
#: Shared with ``plant_board`` — the same 59 that posts filled product onto the
#: finished floor. A goods receipt (16) or a transfer in (67) is stock arriving,
#: not stock made, and counting either would report the same cases twice.
TRANS_TYPE_PRODUCTION_RECEIPT = 59

# ---------------------------------------------------------------------------
# Tonnage
# ---------------------------------------------------------------------------

#: The business's fixed rule. See the module docstring.
LITRES_PER_TON = 1000

# ---------------------------------------------------------------------------
# Intercompany
# ---------------------------------------------------------------------------

#: CardCodes that are the group selling to itself, per correction C-0005.
#:
#: Excluded from dispatch on BOTH sides. Without this, a transfer from Oil to
#: Mart is counted once as an Oil sale and again as Mart's, and the combined
#: figure reports stock that never left the site.
INTERCOMPANY_CARD_CODES = {
    "JIVO_OIL": [
        "CUSTA000001",
        "CUSTA000002",
        "CUSTA000003",
        "CUSTA000004",
        "CUSTA000606",
        "CUSTA000827",
        "CUSTA000906",
        "CUSTA001099",
        "CUSTA001113",
    ],
    "JIVO_MART": [
        "CUSTA000001",
        "CUSTA000827",
        "CUSTA000874",
        "CUSTA000875",
        "CUSTA000876",
        "CUSTA000877",
        "CUSTA000878",
        "CUSTA000926",
    ],
    "JIVO_BEVERAGES": [
        "CUSTA000001",
        "CUSTA000002",
        "CUSTA000003",
        "CUSTA000004",
        "CUSTA000606",
        "CUSTA000827",
    ],
}

#: The companies whose invoices the dispatch tile adds together.
DISPATCH_COMPANIES = ["JIVO_OIL", "JIVO_MART"]

# ---------------------------------------------------------------------------
# The cost donut
# ---------------------------------------------------------------------------

#: The four slices, in fixed order, mapped to the wall board's buckets.
#:
#: The fourth line was called "Others" until 2026-09-15 and IS ELECTRICITY and
#: nothing else — there was never a catch-all behind it. The user renamed it to
#: what it is, so the label no longer needs a footnote explaining itself. The
#: key changed with it; ``others`` is not served any more.
COST_SLICES = [
    {"key": "labour", "bucket": "LABOUR", "label": "Labour"},
    {"key": "electricity", "bucket": "ELECTRICITY", "label": "Electricity"},
    {"key": "salary", "bucket": "SALARY", "label": "Salary"},
    {"key": "maintenance", "bucket": "MAINTENANCE", "label": "Maintenance"},
]

#: The departments the labour slice prices, by name.
#:
#: The Labour Gate register keeps two kinds of row under one shape: a row with
#: NO department is the barrier tally, and a row WITH one is the HOD's split of
#: those same people afterwards. This tile prices the SPLIT rows for the five
#: departments named here, so the labour line answers "what did the floors this
#: board is about cost" rather than "how many bodies crossed the line".
#:
#: That makes the line a SUBSET of the gate by design — Warehouse Gupta, Mess,
#: Ecom, Beverages and everyone never allocated fall outside it, and so does
#: anyone the HOD has not split yet. 629 of the 1,149 gated in for 1-16 Sep
#: 2026. ``AdminBoardService._labour_departments`` states the coverage on the
#: line rather than leaving it to be discovered.
#:
#: Matched on name, case-insensitively, because department IDs differ between
#: deployments and the live master spells it "production(oil)" in lower case.
#: A name with no department behind it is named in a warning, not ignored.
LABOUR_DEPARTMENTS = [
    "production(oil)",
    "Warehouse Basement",
    "Dock",
    "Scrap",
    "Boiling Floor 1",
]

#: Whose meters the electricity slice prices.
#:
#: The Daily Electricity register is CAMPUS-WIDE — Beverages' boiler, ETP, RO
#: and terrace meters are entered on the same page as Oil's — and the factory
#: expense wall deliberately prices every one of them. This board is Jivo Oil's,
#: so its electricity line reads Oil's meters only, and of those the SUB-meters
#: alone: a main measures the supply they slice up, so a total holding both
#: counts the same electricity twice. A sub-meter shared with Beverages counts
#: half. See ``AdminBoardService._electricity_oil`` for both rules.
ELECTRICITY_COMPANY = "JIVO_OIL"

#: The opening words of the factory expense wall's "nobody read a meter"
#: warning, so this board can drop it and raise its own. The wall's fires only
#: when NO meter on the campus was read; a Beverages reading is not evidence
#: that Oil's meters were read, so the wall's silence cannot be trusted here.
ELECTRICITY_WALL_WARNING_PREFIX = "No meter reading entered"

# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

#: Seconds between reads, served on every response so the cadence can be slowed
#: from the server without a frontend release.
REFRESH_SECONDS = 60

#: How many days of production the trend strip carries.
TREND_DAYS = 7


def company_id_for(code: str):
    """The primary key for a company code, or None if this deployment lacks it.

    Returns None rather than raising: a deployment without Jivo Mart should get
    an Oil-only dispatch figure and a named warning, not a 500 on a wall board.
    """
    return Company.objects.filter(code=code).values_list("id", flat=True).first()

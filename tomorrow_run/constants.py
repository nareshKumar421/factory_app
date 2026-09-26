"""The board's rules, as data.

Everything here is transcribed from the planning board the plan was signed off
on (https://jivo-plan-drawings.vercel.app, "From orders to a machine plan") and
its three machine tables (``machine-rules.js``, itself transcribed from
``Machine_Planning_Simple_Tables.pdf`` with Daman's corrections of 13-22 Sept
2026). The engine reads nothing about machines that is not written down here.

Two values the board leaves open are filled in below and said so on the page
(see ``ASSUMED_CHANGES``); change them here when Daman gives the numbers.
"""

# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

DAY_STARTS = "07:30"
SESSION_ENDS = "19:30"
READ_AT = "19:00"
# One session = 12 hours on the clock = 10 hours of running; tea breaks and
# the rest take the other two. Running hours convert to clock hours at 12/10.
SESSION_RUN_H = 10.0
SECOND_SESSION_RUN_H = 20.0
CLOCK_PER_RUN_H = 1.2

TARGET_L = 100_000
GOOD_L = 125_000

# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

# Finished-goods rooms and their limits (your numbers, 14 Sept 2026).
ROOM_LIMIT_L = {"BH-BT": 561_000, "BH-PF": 377_000}
# Production lands in BH-PF; a SKU with no stock anywhere goes there first.
PRODUCTION_ROOM = "BH-PF"
# BH-BT is charged only its own trucks; everything else that leaves the two
# rooms (transfers to Gupta included) leaves from BH-PF (your call, 22 Sept).
TRUCK_ROOM = "BH-BT"
LEFT_DAYS = 7

# The four packaging rooms of the Packing Material dashboard — and nothing
# else. BH-PP is dead junk, never counted (Gurvinder veerji, 22 Sept 2026).
PACKAGING_ROOMS = ("BH-PC", "BH-BS", "BH-PM", "BH-NM")

# Oil I have: the Raw Material Stock page's register (the bulk-oil store).
OIL_ROOM = "BH-LO"
# A count older than this many days is flagged on the page.
OIL_COUNT_STALE_DAYS = 3

# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------

MACHINES = ("JP", "Clear Pack", "10 Head", "6 Head", "Tin", "Hitech pouch", "Samarpan pouch")

# Running speeds, in bottles / containers / tins / pouches per hour — the upper
# value where the notes gave a range. ``None`` means the pack runs there but
# its speed is not recorded, so the plan cannot time it.
SPEEDS = {
    "JP": {"1 L": 4800, "200 ml": None},
    "Clear Pack": {"1 L": 4800, "4 L": 1800, "5 L": 1800},
    "10 Head": {"1 L": 2100, "2 L": 900},
    "6 Head": {"3 L": 780, "4 L": 720, "5 L": 600},
    "Tin": {"15 L": 600},
    "Hitech pouch": {"pouch": 1200},
    "Samarpan pouch": {"pouch": 2100},
}

# Filter 1, the pack: which bottle sizes a machine takes, and in what.
# Clear Pack fills PET bottles only; 5 L tins run on 6 Head; the Tin machine
# runs 15 L ONLY (Daman, 14 Sept 2026).
PACK_TYPES = {
    "JP": ("bottle", "combo"),
    "Clear Pack": ("bottle", "combo"),
    "10 Head": ("bottle", "combo"),
    "6 Head": ("bottle", "tin"),
    "Tin": ("tin",),
    "Hitech pouch": ("pouch",),
    "Samarpan pouch": ("pouch",),
}

# Filter 2, the SKU: the oils a machine runs. ``None`` = every SKU in its packs.
# JP and Clear Pack lists are taken as complete (Daman, 14 Sept 2026); JP runs
# sunflower too (22 Sept 2026). "cold press" on Clear Pack is every cold-press
# SKU, whatever its oil.
MACHINE_OILS = {
    "JP": ("mustard", "pomace", "extra light", "rice bran", "sunflower"),
    "Clear Pack": ("mustard", "rice bran", "cold press"),
    "10 Head": None,
    "6 Head": None,
    "Tin": None,
    "Hitech pouch": None,
    "Samarpan pouch": None,
}

# Table 3, changeovers, in running hours. JP's is "5 to 6 hours"; the plan
# takes 6. ``SLOW_FROM`` oils need the slower change when they come OFF the
# machine.
CHANGE_H = {
    "JP": 6.0,
    "Clear Pack": 1.0,
    "10 Head": 0.5,
    "6 Head": 0.5,
    "Tin": 0.75,
    "Hitech pouch": 0.75,
    "Samarpan pouch": 0.75,
}
CLEAR_PACK_FROM_MUSTARD_H = 2.0          # 1 h normal + 1 h extra flushing
HEADS_SLOW_FROM = ("mustard", "groundnut", "extra virgin olive")
# The board says "more than 1 hour" with no upper bound, and "0.75 h flushing"
# for rice bran to cold press with no total. The plan takes these; the page
# lists them under "What is assumed" until Daman gives the numbers.
HEADS_SLOW_H = 1.5
CLEAR_PACK_RICE_BRAN_TO_COLD_PRESS_H = 1.25
ASSUMED_CHANGES = (
    "10 Head and 6 Head, from mustard, groundnut or extra virgin olive to another SKU: "
    "the board says more than 1 hour and gives no upper bound; the plan takes 1.5 h.",
    "Clear Pack, rice bran to cold press: the board gives 0.75 h of flushing and no total; "
    "the plan takes 1.25 h (0.75 h flushing + the normal 0.5 h of setup).",
)

# What the planning sheet calls each machine, and the machines it means.
SHEET_MACHINE_WORDS = (
    ("clearpack", ("Clear Pack",)),
    ("clear pack", ("Clear Pack",)),
    ("jp", ("JP",)),
    ("10 head", ("10 Head",)),
    ("6 head", ("6 Head",)),
    ("tin", ("Tin",)),
    ("hitech", ("Hitech pouch",)),
    ("samarpan", ("Samarpan pouch",)),
    ("pouch", ("Hitech pouch", "Samarpan pouch")),
)

# The factory app's production line each machine logs its runs on. The two
# pouch machines share one line, so the log cannot say which of them ran.
MACHINE_LINES = {
    "JP": ("jp machine", "jp"),
    "Clear Pack": ("clear pack",),
    "10 Head": ("10 head",),
    "6 Head": ("6 head",),
    "Tin": ("tin head", "tin"),
}
RUNNING_NOW_DAYS = 7

# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

# One bottle, two carton codes: stock, orders and what was made of both are
# counted together and planned under the first; when its carton runs out the
# rest is made under the second (Daman, 15 + 22 Sept 2026).
CARTON_TWINS = (
    ("FG0000142", "FG0000461", "Cold Press Groundnut 1 Ltr", "15 Sept"),
    ("FG0000028", "FG0000466", "Pomace Olive 1 Ltr", "22 Sept"),
    ("FG0000005", "FG0000462", "Extra Light Olive 1 Ltr", "22 Sept"),
    ("FG0000081", "FG0000456", "Cold Press Sunflower 1 Ltr", "22 Sept"),
)

# Codes the planning sheet writes in another book's numbering (Jivo Mart's),
# and the Oil item that makes that product — from the recipe map, every pair
# pack-checked (15 Sept 2026). A sheet line whose name states a different size
# from the Oil item under its code, and is not listed here, is not planned:
# it is shown as a warning so the planning team can put the Oil code in.
SHEET_CODE_ALIASES = {
    "FG0000420": {"planned_as": "FG0000441", "book": "Jivo Mart", "basis": "the recipe map"},
    "FG0000437": {"planned_as": "FG0000349", "book": "Jivo Mart", "basis": "the recipe map"},
}

# Oil families, first match wins, so the longer names come first. Matched on
# the item name because the floor's words are what the machine lists use:
# SAP's variety field files pomace, extra light and extra virgin all as olive,
# and JP runs two of those three.
OIL_WORDS = (
    ("yellow mustard", "yellow mustard"),
    ("mustard", "mustard"),
    ("kachi ghani", "mustard"),
    ("kacchi ghani", "mustard"),
    ("kachhi ghani", "mustard"),
    ("extra light", "extra light"),
    ("extra virgin coconut", "extra virgin coconut"),
    ("extra virgin olive", "extra virgin olive"),
    ("extra virgin", "extra virgin olive"),
    ("pomace", "pomace"),
    ("so olive", "olive"),
    ("rice bran", "rice bran"),
    ("sunflower", "sunflower"),
    ("groundnut", "groundnut"),
    ("soyabean", "soyabean"),
    ("sesame", "sesame"),
    ("cotton seed", "cotton seed"),
    ("canola", "canola"),
    ("gold", "jivo gold"),
    ("ghee", "desi ghee"),
    ("coconut", "coconut"),
    ("refined", "refined"),
)

# How a packaging item that ran short is named in "Waiting for another day".
PACKAGING_WORDS = (
    ("pouch", "no pouch film"),
    ("bottle", "no bottles"),
    ("jar", "no bottles"),
    ("cap", "no caps"),
    ("label", "no labels"),
    ("carton", "no cartons"),
    ("tin", "no tins"),
    ("shrink", "no packaging"),
    ("tape", "no packaging"),
)

PICKERS = ("Gurvinder veerji", "Daman")
PICK_REASONS = (
    "Urgent order",
    "Saves a changeover",
    "Material is ready",
    "Stock is running low",
    "Machine or staff",
)

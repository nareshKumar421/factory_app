"""
production_dispatch/constants.py

The report's parameters -- the "Notes" sheet of the workbook it replaces
("Production & Dispatch- PALLET Jul-Sep 2026.xlsx"), fixed here rather than
typed into yellow cells.
"""

from admin_board.constants import INTERCOMPANY_CARD_CODES

#: The report is Oil's: the pallet size and the oil density below are Oil's,
#: and it is read from Oil's schema whichever company the reader is signed into.
COMPANY_CODE = "JIVO_OIL"
COMPANY_LABEL = "Jivo Oil"

#: Litres on one pallet. PALLET = litres / this, as in the workbook.
PALLET_LITRES = 800

#: kg of oil in one litre. TON = litres x this / 1000 -- net oil weight, the
#: workbook's rule (13 KGS = 14.2857 L). Deliberately NOT ``U_Gross_Weight``,
#: which the Admin and Logistics boards weigh with: that is a packed case's
#: gross weight, and it is missing or inconsistent on about 30 FG items.
OIL_DENSITY = 0.91

#: A SKU is FAST when one month of its production is dispatched within this
#: many days, otherwise SLOW.
FAST_DAYS = 30

#: Days in "a month" for the average monthly production. 30, so the 90-day
#: window is exactly three months.
MONTH_DAYS = 30

#: FAST / SLOW is judged over the days ending on the report's To date, whatever
#: range is being read, so a SKU's label does not flip from one day to the next.
MOVEMENT_WINDOW_DAYS = 90

#: The longest range one read may cover.
MAX_RANGE_DAYS = 366

#: Sales to these customers are group companies' -- Jivo Mart above all, which
#: took 61% of the dispatched pallets in Jul-Sep 2026 -- and are not counted
#: as dispatch. The same list the Admin board leaves out of its dispatch tile.
GROUP_CARD_CODES = list(INTERCOMPANY_CARD_CODES.get(COMPANY_CODE, []))

#: SAP's spellings of a packing type that mean one of the others. "HDFPE" is a
#: typo on every 5-litre jar in the item master; the workbook read it as HDPE.
PACKING_TYPE_SPELLINGS = {"HDFPE BOTTLE": "HDPE BOTTLE"}

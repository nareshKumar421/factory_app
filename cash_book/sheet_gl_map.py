"""
The sheet's G/L words, resolved to SAP accounts.

The cash sheet writes its heads as words -- "Refreshment", "R&M", "Advacne" --
with the spelling of whoever was holding the pen. This module holds SAP account
codes, so each word has to become one. The codes below were read out of
``JIVO_OIL_HANADB.OACT`` (postable accounts only) on 15 September 2026.

Keys are lower-cased and stripped, so the sheet's own variants collapse:
"Wg"/"wg"/"WG", "Freight"/"Fright", "Housekeeping"/"Hosekeeping",
"Conveyance"/"Conveyace".

CERTAIN vs JUDGEMENT
--------------------
An entry marked ``certain=True`` has a SAP account of the same name -- there is
nothing to decide. One marked ``certain=False`` does not, and somebody picked
the nearest head; those are printed by ``import_cash_sheet --dry-run`` so they
can be argued with before 485 rows are filed against them.

Receipts need no head at all. Money arriving in the box is not spent yet, so
the sheet's "Cash" head has no account here and the importer ignores the word
on any row with an amount in the In column.
"""

#: word -> (account code, account name, certain)
GL_ACCOUNTS = {
    # --- exact matches in SAP's chart -------------------------------------
    "refreshment": ("5630004", "REFRESHMENT", True),
    "staffwellfair": ("5630003", "STAFF WELFARE", True),
    "r&m": ("5650016", "REPAIR AND MAINTENANCE PLANT & MACHINERY", True),
    "conveyance": ("5690002", "CONVEYANCE", True),
    "conveyace": ("5690002", "CONVEYANCE", True),
    "housekeeping": ("5680015", "HOUSE KEEPING", True),
    "hosekeeping": ("5680015", "HOUSE KEEPING", True),
    "printing & stationery": ("5680012", "PRINTING AND STATIONERY", True),
    "courier": ("5680023", "POSTAGE & COURIER", True),
    "internet": ("5680003", "TELEPHONE MOBILE AND INTERNET", True),
    "bank charge": ("5610003", "BANK CHARGES", True),
    "lab & testing": ("5680013", "LAB AND TESTING", True),
    "legal documets": ("5680025", "LEGAL AND PROFESSIONAL", True),
    "unloading": ("5670002", "UNLOADING/LOADING CHARGES-INDIRECT EXPENSE", True),
    "freight": ("5670001", "FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP", True),
    "fright": ("5670001", "FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP", True),
    "fuel": ("5650015", "FUEL - VEHICLES", True),
    "direct exp.": ("5100015", "CONSUMABLE/DIRECT EXPENSE", True),
    "room rent": ("5660002", "RENT", True),

    # --- judgement calls, no SAP head of that name ------------------------
    # 67 rows, all "material dispatch" against a vehicle number, mostly Mart.
    # Outward carriage. Could equally be 5670003 DELIVERY CHARGES ON CASH
    # SALE, but these are dispatches against invoices, not cash sales.
    "delivery": ("5670001", "FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP", False),
    # Ghaziabad/Delhi/Faridabad -> factory, so carriage inward rather than out.
    "porter": ("5680028", "FREIGHT INWARD-INDIRECT", False),
    # Advances against salary. SAP keeps a per-employee account
    # ("SHAHRUKH KHAN ADVANCE JWPL2885" and ~3,000 more), and the detail names
    # the person on every row -- one even carries the code, JWPL2175. Pointed
    # at the general staff-debtor head rather than guessing 14 individuals.
    "advacne": ("1101015", "SUNDRY DEBTORS STAFF", False),
    # A hot gun, a printer cable and the like, booked to F/A on the sheet.
    "f/a": ("1205001", "ELECTRICAL APPLIANCES", False),
    # An air-conditioner fitted at site, and a gate fitted to the G.C room.
    "installation": ("5650001", "REPAIR & MAINTENANCE OFFICE & BUILDING", False),
    "g.c room": ("5650001", "REPAIR & MAINTENANCE OFFICE & BUILDING", False),
    # Salary increments handed over in cash.
    "increment": ("5630001", "SALARY EXPENSE", False),
    # A litre of oil drawn for checking. SAP has no "sample" head.
    "sample": ("5680013", "LAB AND TESTING", False),
    "oil": ("5680013", "LAB AND TESTING", False),
    # One A4 print run for a court filing.
    "printer": ("5680012", "PRINTING AND STATIONERY", False),
    # Hydra (mobile crane) hired to shift a company car.
    "hydra": ("5650002", "REPAIR & MAINTENANCE VEHICLE", False),
    # Green tax paid on a company vehicle entering Delhi.
    "receive": ("5650002", "REPAIR & MAINTENANCE VEHICLE", False),
}

#: Words that only ever appear on a receipt. Listed so an unmapped-head report
#: does not flag them, and so a *payment* written against one is still caught.
RECEIPT_WORDS = {"cash"}


def resolve(word):
    """``(code, name, certain)`` for a sheet G/L word, or ``None`` if unknown."""
    return GL_ACCOUNTS.get((word or "").strip().lower())


def uncertain_words():
    """The judgement calls, for the dry run to print."""
    return sorted(
        {word for word, (_, _, certain) in GL_ACCOUNTS.items() if not certain}
    )

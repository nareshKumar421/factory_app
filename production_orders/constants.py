"""What SAP's own set-up fixes about a production order entry, per company.

Read from live SAP on 2026-10-09 (see ``docs/README.md``). The SAP screen fills
some of these by itself — a formatted search, a UDF default — and the Service
Layer does not, so the app has to send them.
"""

from decimal import Decimal

#: Companies this module posts for. Oil's finished-goods filling comes first.
SUPPORTED_COMPANIES = ("JIVO_OIL",)

#: ``OITM.ItmsGrpCod`` of the finished goods a filling entry produces.
FG_ITEM_GROUP = {"JIVO_OIL": 102}

#: ``OWOR.CardCode`` on every order: Oil's production-order screen sets it with
#: a formatted search (query 516, "Production Vendor").
WIP_CARD_CODE = {"JIVO_OIL": "VENDA001625"}

#: The batch number starts with the line the oil was filled on. ``M`` is the
#: small packs filled by hand.
LINE_CODES = {
    "L1": "Line 1 (Clear Pack)",
    "L2": "Line 2 (JP Machine)",
    "L3": "Line 3 (10 Head)",
    "L4": "Line 4 (6 Head)",
    "L5": "Line 5 (Tin Head)",
    "M": "Manual (small packs)",
}

#: The oil code in the batch number: six digits, given by whoever received the
#: oil (in the plant's WhatsApp group).
OIL_CODE_LENGTH = 6

#: ``OBTN.DistNumber`` is 36 characters.
BATCH_NUMBER_MAX_LENGTH = 36

#: Expiry is the production date plus this many years, less a day.
SHELF_LIFE_YEARS = 2

#: ``Comments`` on OWOR / OIGE / OIGN is 254 characters.
COMMENTS_MAX_LENGTH = 254

#: SAP's stock caps (SBO_SP_TransactionNotification, errors 20204, 20275952,
#: 20275953, 202014 and 59295). Each sums ``OnHand × SalPackUn`` of the litre
#: items (``U_IsLitre = 'Y'``) in the warehouses named, over the items picked
#: by ``series`` (``OITM.Series``, 389 = FG) or ``group`` (``ItmsGrpCod``).
#: ``on_order`` caps refuse any add or update of a standard FG order, so both
#: creating it and closing it; ``on_receipt`` ones refuse a receipt into BH-PF.
#: ``with_entry`` adds this entry's litres before comparing.
STOCK_CAPS = {
    "JIVO_OIL": [
        {
            "key": "pf_fg",
            "label": "BH-PF finished goods",
            "warehouses": ("BH-PF",),
            "series": 389,
            "limit": Decimal("350000"),
            "on_order": True,
            "on_receipt": False,
            "with_entry": False,
        },
        {
            "key": "pf_fu",
            "label": "BH-PF and BH-FU finished goods",
            "warehouses": ("BH-PF", "BH-FU"),
            "group": 102,
            "limit": Decimal("350000"),
            "on_order": True,
            "on_receipt": False,
            "with_entry": False,
        },
        {
            "key": "ec",
            "label": "BH-EC finished goods",
            "warehouses": ("BH-EC",),
            "group": 102,
            "limit": Decimal("350000"),
            "on_order": True,
            "on_receipt": False,
            "with_entry": False,
        },
        {
            "key": "godowns",
            "label": "BH-FG, BH-FU, BH-EC and GP-FG finished goods",
            "warehouses": ("BH-FG", "BH-FU", "BH-EC", "GP-FG"),
            "series": 389,
            "limit": Decimal("2200000"),
            "on_order": True,
            "on_receipt": False,
            "with_entry": True,
        },
        {
            # 20295: checked while the order is planned (created, or changed
            # while planned), with this order's litres added.
            "key": "pf_planned",
            "label": "BH-PF finished goods, with this order",
            "warehouses": ("BH-PF",),
            "group": 102,
            "limit": Decimal("350000"),
            "on_order": True,
            "on_receipt": False,
            "with_entry": True,
        },
        {
            "key": "pf_receipt",
            "label": "BH-PF finished goods",
            "warehouses": ("BH-PF",),
            "group": 102,
            "limit": Decimal("350000"),
            "on_order": False,
            "on_receipt": True,
            "with_entry": False,
        },
    ],
}

#: SAP object codes (``NNM1.ObjectCode``) for the month's numbering series.
OBJECT_PRODUCTION_ORDER = "202"
OBJECT_ISSUE_FOR_PRODUCTION = "60"
OBJECT_RECEIPT_FROM_PRODUCTION = "59"

#: Quantities are compared to this many decimals (SAP keeps six).
QUANTITY_PLACES = Decimal("0.000001")

#: ``OWOR.Status`` and ``OWOR.Type``, for the list of SAP's own orders.
SAP_ORDER_STATUSES = {"P": "Planned", "R": "Released", "L": "Closed", "C": "Cancelled"}
SAP_ORDER_TYPES = {"S": "Standard", "P": "Special", "D": "Disassembly"}

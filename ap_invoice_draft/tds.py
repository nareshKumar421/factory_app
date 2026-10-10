"""TDS on the draft: what accounts' own "Copy To A/P Invoice" puts on.

TDS (tax deducted at source) is income tax Jivo keeps back from a vendor's bill
and pays to the government in the vendor's name. On goods it is SAP withholding
code 1031, "Purchase of Goods": 0.1% of the bill before GST, once the year's
purchases from the vendor pass ₹50 lakh. The rate and the limit are read from
SAP (``OWHT``); the code is 1031 in all three companies. A vendor takes it when
its master is TDS-liable (``OCRD.WTLiable``) and lists 1031 (``CRD4``).

SAP's desktop client ticks it from the vendor master; Service Layer copies the
GRPO lines as they are and adds nothing, so the draft has to carry it: every
line ``WTLiable`` and the code in ``WithholdingTaxDataCollection``. Either alone
leaves the TDS at 0 (tried on TEST 2026-10-10); with both, SAP works out the
amount itself.

The year runs April to March and counts the vendor's posted A/P invoices less
its A/P credit notes, before GST. That gives back the base accounts used on BR
Agrotech's crossing bill (626084298) to within ₹1.27.

The bill that crosses the limit owes TDS only on the part above it. Accounts
enter the rest as the invoice's "WTax non-subject amount"; Service Layer ignores
that field, and a part ``TaxableAmount`` makes SAP re-cut the GST (₹13.28 more
tax and a matching discount on a TEST draft). So the draft deducts on the whole
bill, as accounts did on Kuber Paper's crossing bill (626094309), and the note
says what the part is for accounts to set in SAP.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from .checks import _money

#: SAP withholding code for TDS on the purchase of goods.
GOODS_TDS_CODE = "1031"


@dataclass(frozen=True)
class TdsDecision:
    #: The withholding code the draft carries; blank when it carries none.
    code: str = ""
    #: What the TDS is worked out on: the bill before GST.
    taxable: Optional[Decimal] = None
    #: Why, in a sentence for the page.
    note: str = ""


def financial_year(day: date) -> tuple[date, date]:
    """The April-to-March year ``day`` falls in."""
    start = day.year if day.month >= 4 else day.year - 1
    return date(start, 4, 1), date(start + 1, 3, 31)


def decide(setup: Optional[dict], bill: Decimal) -> TdsDecision:
    """TDS for a bill of ``bill`` (before GST), from the vendor's ``setup`` as
    ``GRPOReader.goods_tds`` reads it; ``None`` when SAP has no such vendor."""
    if not setup or not setup["liable"]:
        return TdsDecision(note="No TDS: SAP has this vendor as not liable for TDS.")
    if not setup["allowed"]:
        return TdsDecision(
            note=f"No TDS: the vendor's master in SAP does not list {GOODS_TDS_CODE} (Purchase of Goods)."
        )
    if not setup["active"]:
        return TdsDecision(note=f"No TDS: SAP has no active withholding code {GOODS_TDS_CODE}.")

    rate = format(Decimal(setup["rate"]).normalize(), "f")
    limit = setup["threshold"]
    before = setup["year_to_date"]
    if limit <= 0 or before >= limit:
        return TdsDecision(
            code=GOODS_TDS_CODE,
            taxable=bill,
            note=f"TDS {GOODS_TDS_CODE} at {rate}%: purchases from this vendor this year were "
                 f"already {_money(before)}, past the {_money(limit)} limit.",
        )
    if before + bill > limit:
        return TdsDecision(
            code=GOODS_TDS_CODE,
            taxable=bill,
            note=f"TDS {GOODS_TDS_CODE} at {rate}% on the whole bill. This bill takes the year's "
                 f"purchases from this vendor past {_money(limit)} ({_money(before)} before it), "
                 f"so TDS is due only on the {_money(before + bill - limit)} above the limit: "
                 f"accounts can set that as the WTax base in SAP before adding the draft.",
        )
    return TdsDecision(
        note=f"No TDS yet: purchases from this vendor this year come to {_money(before + bill)} "
             f"with this bill, within the {_money(limit)} limit."
    )

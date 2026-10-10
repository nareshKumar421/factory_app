"""The fixed numbers and words of cash book -> SAP.

Everything a person may want to change lives on a routing row or a setting
(``flags.py``); what is here is SAP's own limits and the app's own vocabulary.
"""

from cash_book.constants import SALARY_ADVANCE_GL_CODES

#: OPOR/OPDN/OPCH ``Comments`` is NVARCHAR(254).
COMMENTS_MAX = 254
#: PCH1 ``Dscription`` on a service line.
ITEM_DESCRIPTION_MAX = 100
#: ``NumAtCard`` is NVARCHAR(100).
NUM_AT_CARD_MAX = 100
#: How many ``.`` a vendor reference may grow before the app gives up and asks
#: a person. SAP users add a trailing dot to dodge the duplicate-reference
#: check (``JUN 26/51325/1716.``); the app does the same, deterministically.
MAX_DOT_SUFFIX = 5

#: SAP object codes (ObjType): purchase order, goods receipt PO, A/P invoice.
OBJ_PO, OBJ_GRPO, OBJ_AP = "22", "20", "18"

#: sap_postings kinds. Registered in ``CashSapConfig.ready()``.
KIND_PO = "cash_sap.po"
KIND_GRPO = "cash_sap.grpo"
KIND_AP = "cash_sap.ap_draft"
KIND_PO_CLOSE = "cash_sap.po_close"

#: SAP evidence: a carriage return (no line feed) before "G.no." in 46 of the
#: 49 imprest GRPOs USER22 keyed between August and October 2026.
GNO_SEPARATOR = "\r"

#: Heads that never go to SAP from the cash book, whatever the map says: an
#: advance against salary is a debtor, not a purchase.
NEVER_POSTED_GL_CODES = tuple(SALARY_ADVANCE_GL_CODES)

#: Marker prefixes, one per document the app makes.
MARKER_PO = "PO "
MARKER_GRPO = "G"
MARKER_AP_ITEM = "API "
MARKER_AP_SERVICE = "APS "

#: The Comments marker that makes every document findable again (spec D7).
MARKER_TEMPLATE = "[JI-CB {prefix}{row_id}{round}]"

#: Problem codes, so the screen and the tests name the same thing.
BUNCH_CHANGED = "BUNCH_CHANGED"
#: The voucher's one ``ImprestVoucher`` row is committed (or left out after a
#: PO) under another bunch: the cash book moved it, and posting it from here
#: too would put it in SAP twice.
IN_OTHER_BUNCH = "IN_OTHER_BUNCH"
#: A goods voucher that is not on the batch's committed PO.
NOT_ON_PO = "NOT_ON_PO"
#: A service voucher that is not on the batch's committed SERVICE A/P draft.
NOT_ON_AP_DRAFT = "NOT_ON_AP_DRAFT"
#: A PO or SERVICE draft not yet committed whose frozen vouchers differ from
#: what the plan would freeze now (an override, the bill facts, the settings
#: or an accepted change). It is frozen again before it is sent.
CHANGED_SINCE_FREEZE = "CHANGED_SINCE_FREEZE"

#: A voucher older than this many days gets a "check the date" warning.
VOUCHER_AGE_WARNING_DAYS = 45


def marker(prefix: str, row_id: int, round_: int = 1) -> str:
    """``[JI-CB PO 42]``, ``[JI-CB PO 42-2]``, ``[JI-CB G77]``.

    The round is added only after a cancel-and-re-raise, so a cancelled or
    closed SAP document carrying the old marker is never adopted again.
    """
    suffix = f"-{int(round_)}" if round_ and int(round_) > 1 else ""
    return MARKER_TEMPLATE.format(prefix=prefix, row_id=int(row_id), round=suffix)

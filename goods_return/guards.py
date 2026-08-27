"""Pre-flight checks for a customer goods return, run before SAP is called.

Returns are worth guarding harder than most documents for one reason: **the app
cannot undo one.** SAP restricts cancelling an A/R Return to a named list of
users (errors 160002/160010) and the app's Service Layer user is not among them —
verified by a live `Cancel` that came back `-1116`. A wrongly posted return needs
a person in SAP to fix it, so anything checkable is checked here first.

Every rule below was read from `SBO_SP_TRANSACTIONNOTIFICATION` in
`JIVO_OIL_HANADB` or established by posting into `TEST_JIVO_OIL_HANADB`. The
error numbers are kept in the messages so a refusal pasted to the SAP team can be
traced to its rule.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

# OCRD.GroupCode for internal branch customers (error 160012).
BRANCH_CUSTOMER_GROUP = 100

# Companies whose notification procedure refuses a return dated outside the
# current calendar month. Oil is error 160027 (exempt only UserSign 25),
# Beverages is 160026 with no exemption at all; Mart has no such rule. The app
# posts as B1i (UserSign 2), so neither exemption is available to it — and since
# a current-month date is valid everywhere, this is enforced for all companies
# rather than tracked per company.
CURRENT_MONTH_ONLY = True


class GoodsReturnGuardError(ValueError):
    """A return SAP would refuse, refused earlier and in words."""


def check_posting_date(posting_date) -> None:
    """SAP refuses a return dated outside the current month (160027 / 160026).

    This bites when goods arrive late in one month and are received early in the
    next: the document has to carry the current month's date, so the physical
    arrival date and the SAP date can legitimately differ.
    """
    if not CURRENT_MONTH_ONLY or posting_date is None:
        return
    import datetime

    today = datetime.date.today()
    if (posting_date.year, posting_date.month) != (today.year, today.month):
        raise GoodsReturnGuardError(
            f"SAP only accepts a return note dated in the current month, so "
            f"{posting_date:%d %b %Y} cannot be used (error 160027). Post it with "
            f"today's date, or ask the SAP team to book it manually."
        )


def check_customer(card_code: str, group_code: Optional[int]) -> None:
    """A branch is not a customer — its stock comes back by transfer (160012)."""
    if not (card_code or "").strip():
        raise GoodsReturnGuardError("A return needs the customer it came back from.")
    if group_code == BRANCH_CUSTOMER_GROUP:
        raise GoodsReturnGuardError(
            f"{card_code} is an internal branch, and SAP does not allow a return "
            f"against one (error 160012). Move the stock back with a branch "
            f"transfer instead."
        )


def check_reference(num_at_card: str) -> str:
    """SAP requires the customer reference in upper case (160007).

    Returned upper-cased rather than merely rejected: the case of a reference
    carries no meaning, so silently correcting it is kinder than refusing.
    """
    return (num_at_card or "").strip().upper()[:100]


def check_warehouse(warehouse_code: str, branch_id: Optional[int]) -> None:
    """Every line needs a warehouse (160017) and the header needs its branch."""
    if not (warehouse_code or "").strip():
        raise GoodsReturnGuardError("Select the goods-return warehouse.")
    if branch_id is None:
        raise GoodsReturnGuardError(
            f"{warehouse_code} has no branch set in SAP, and a return cannot be "
            f"posted without one (SAP: 'Specify an active branch'). Ask the SAP "
            f"team to set BPLid on the warehouse."
        )


def check_lines(
    lines: Iterable[dict],
    *,
    variety_codes: dict[str, str],
    tax_codes: dict[str, str],
    return_costs: dict[str, Decimal],
) -> None:
    """Everything SAP demands per line, checked against what we resolved.

    Each line is a dict with at least `item_code` and `quantity`.
    """
    lines = list(lines)
    if not lines:
        raise GoodsReturnGuardError("This return has no items to post.")

    # 160020: SAP refuses duplicate item lines and asks for them to be merged.
    seen: set[str] = set()
    for line in lines:
        item = (line.get("item_code") or "").strip()
        quantity = Decimal(str(line.get("quantity") or 0))

        if not item:
            raise GoodsReturnGuardError("A return line has no item code.")

        if item in seen:
            raise GoodsReturnGuardError(
                f"{item} appears on more than one line. SAP requires the "
                f"quantities combined into a single line (error 160020)."
            )
        seen.add(item)

        # 160014
        if quantity <= 0:
            raise GoodsReturnGuardError(
                f"{item} has a return quantity of {quantity}; it must be positive."
            )

        # 160013 / 160015
        if not variety_codes.get(item):
            raise GoodsReturnGuardError(
                f"{item} has no Variety mapped in SAP, which a return line must "
                f"carry (error 160013). Its item master needs a Sub Group that "
                f"matches a Dimension 1 profit centre."
            )

        # 160009
        if not tax_codes.get(item):
            raise GoodsReturnGuardError(
                f"No tax code could be found for {item} — this customer has never "
                f"been billed for it, and SAP requires one on every return line "
                f"(error 160009)."
            )

        # 160021. This is the figure that values the returned stock: SAP posts
        # ReturnCost x Quantity as the inventory value, so a zero would bring the
        # goods back at nothing and quietly understate inventory.
        cost = return_costs.get(item)
        if not cost or Decimal(str(cost)) <= 0:
            raise GoodsReturnGuardError(
                f"No cost is known for {item}, so the returned stock cannot be "
                f"valued (error 160021). It has no costed movement in SAP yet."
            )


def batch_number_for(entry_no: str, line_num: int) -> str:
    """A fresh batch number for a returned line.

    SAP will not let a return receive into an existing batch — it refuses with
    `10001226 Batch <n> ... already exists` even when that batch sits in another
    warehouse — so a return necessarily creates one. Deriving it from the app's
    own entry number keeps it unique and traceable back to this record; the
    customer's original batch is preserved as line text instead.
    """
    return f"{entry_no}-{line_num}".upper()[:36]

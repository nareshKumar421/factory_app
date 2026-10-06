"""
amounts_board/services.py

The Amounts board: what the two plants hold, in rupees, and what customers owe.

    Oil plant       total (RM | PM | FG) · RM owner · PM owner · FG owner · non-moving
    Beverage plant  the same
    Debtors         JWPL · MART · Beverages · Total

Composed server-side across all three company schemas, so the figure does not
depend on which company the reader is signed into.

EVERY SAP READ IS ITS OWN SECTION
---------------------------------
Each plant's stock, each plant's non-moving figure and each company's debtors
can fail alone and come back ``None``, named in ``meta.degraded``. The owners
come out of Postgres and are not a section at all: when SAP is down the tiles
still say whose stock it is, which is the half of the tile nobody needs SAP for.

The debtors Total is the sum of the companies that DID read. When one did not,
the Total names it in ``missing`` rather than quietly reporting two companies'
worth under the heading of three.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from django.utils import timezone

from admin_board.constants import INTERCOMPANY_CARD_CODES
from control_boards.sections import SectionBuilder
from non_moving_rm.services import NonMovingRMService
from sap_client.context import CompanyContext
from warehouse.models_manager import UserWarehouse

from .constants import (
    AMOUNTS_BOARD_REFRESH_SECONDS,
    CATEGORIES,
    CATEGORY_ITEM_GROUP,
    DEBTOR_COMPANIES,
    NON_MOVING_AGE_DAYS,
    NON_MOVING_ITEM_GROUP,
    NON_MOVING_WAREHOUSES,
    OLDEST_DEBT_FLOOR,
    PLANT_COMPANIES,
    PLANT_LABELS,
)
from .hana_reader import AmountsReader
from .models import StockOwner


def _money(value) -> float:
    return round(float(value or 0), 2)


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def owner_payload(owner: Optional[StockOwner]) -> Optional[Dict[str, Any]]:
    if owner is None:
        return None
    return {"id": owner.user_id, "name": owner.user.full_name, "email": owner.user.email}


def owners_by_company(company_codes) -> Dict[str, Dict[str, StockOwner]]:
    """``{company_code: {category: StockOwner}}`` for the given companies."""
    found: Dict[str, Dict[str, StockOwner]] = defaultdict(dict)
    rows = StockOwner.objects.filter(company__code__in=list(company_codes)).select_related(
        "company", "user"
    )
    for row in rows:
        found[row.company.code][row.category] = row
    return found


def managers_by_godown(company_code: str) -> Dict[str, List[str]]:
    """``{warehouse_code: [names]}`` from Admin -> Warehouse Managers."""
    found: Dict[str, List[str]] = defaultdict(list)
    rows = (
        UserWarehouse.objects.filter(company__code=company_code, is_active=True, user__is_active=True)
        .select_related("user")
        .order_by("warehouse_code", "user__full_name")
    )
    for row in rows:
        found[row.warehouse_code].append(row.user.full_name or row.user.email)
    return found


class AmountsBoardService(SectionBuilder):
    """Builds the whole board. Collaborators are injectable for tests."""

    def __init__(
        self,
        user=None,
        reader_factory: Callable[[str], AmountsReader] | None = None,
        non_moving_factory: Callable[[str], NonMovingRMService] | None = None,
    ):
        self.user = user
        self._reader_factory = reader_factory or (lambda code: AmountsReader(CompanyContext(code)))
        self._non_moving_factory = non_moving_factory or NonMovingRMService
        self._readers: Dict[str, AmountsReader] = {}

    def _reader(self, company_code: str) -> AmountsReader:
        if company_code not in self._readers:
            self._readers[company_code] = self._reader_factory(company_code)
        return self._readers[company_code]

    # ------------------------------------------------------------------
    # The board
    # ------------------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        self._init_sections()
        owners = owners_by_company(PLANT_COMPANIES)

        plants = []
        for code in PLANT_COMPANIES:
            slug = code.lower()
            plants.append(
                {
                    "company_code": code,
                    "label": PLANT_LABELS[code],
                    "owners": {
                        category.value: owner_payload(owners.get(code, {}).get(category.value))
                        for category in CATEGORIES
                    },
                    "stock": self.section(f"{slug}_stock", lambda c=code: self.plant_stock(c)),
                    "non_moving": self.section(
                        f"{slug}_non_moving", lambda c=code: self.non_moving(c)
                    ),
                }
            )

        return {
            "plants": plants,
            "debtors": self.debtors(),
            "meta": {
                "generated_at": timezone.now().isoformat(),
                "refresh_seconds": AMOUNTS_BOARD_REFRESH_SECONDS,
                **self.section_meta(),
            },
        }

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    def plant_stock(self, company_code: str) -> Dict[str, Any]:
        """The plant's RM / PM / FG value, each with the godowns holding it."""
        group_category = {group: category for category, group in CATEGORY_ITEM_GROUP.items()}
        rows = self._reader(company_code).stock_by_godown(sorted(group_category))
        managers = managers_by_godown(company_code)

        godowns: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            category = group_category.get(int(row["ItemGroup"]))
            if category is None:
                continue
            code = (row["Warehouse"] or "").strip()
            godowns[category].append(
                {
                    "code": code,
                    "name": row["WarehouseName"] or code,
                    "value": _money(row["Value"]),
                    "items": int(row["Items"] or 0),
                    "managers": managers.get(code.upper(), []),
                }
            )

        categories = []
        for category in CATEGORIES:
            listed = sorted(godowns[category.value], key=lambda g: (-g["value"], g["code"]))
            categories.append(
                {
                    "key": category.value,
                    "label": category.label,
                    "item_group": CATEGORY_ITEM_GROUP[category],
                    "value": _money(sum(g["value"] for g in listed)),
                    "items": sum(g["items"] for g in listed),
                    "godowns": listed,
                }
            )

        return {
            "total": _money(sum(c["value"] for c in categories)),
            "categories": categories,
        }

    def godown_items(self, company_code: str, category: str, warehouse: str) -> Dict[str, Any]:
        """The stock inside one godown, for one category -- the drill's last level."""
        group = CATEGORY_ITEM_GROUP[category]
        rows = self._reader(company_code).godown_items(warehouse, group)
        items = [
            {
                "item_code": row["ItemCode"],
                "item_name": row["ItemName"],
                "uom": row["Uom"],
                "quantity": round(float(row["Quantity"] or 0), 3),
                "value": _money(row["Value"]),
            }
            for row in rows
        ]
        return {
            "company_code": company_code,
            "category": category,
            "warehouse": warehouse,
            "value": _money(sum(i["value"] for i in items)),
            "items": items,
        }

    # ------------------------------------------------------------------
    # Non-moving
    # ------------------------------------------------------------------

    def non_moving(self, company_code: str) -> Dict[str, Any]:
        """The figure the Non-Moving page opens on, for this company.

        Same call and same scope as that page's first screen -- packing
        material idle more than 45 days in its four default stores -- so the
        number on the tile is the number the click lands on.
        """
        report = self._non_moving_factory(company_code).get_report(
            age=NON_MOVING_AGE_DAYS, item_group=NON_MOVING_ITEM_GROUP
        )
        scope = set(NON_MOVING_WAREHOUSES)
        rows = [
            r for r in (report.get("data") or []) if (r.get("warehouse") or "").strip().upper() in scope
        ]
        return {
            "value": _money(sum(float(r.get("value") or 0) for r in rows)),
            "item_count": len({r.get("item_code") for r in rows if r.get("item_code")}),
            "warehouses": sorted({(r.get("warehouse") or "").strip().upper() for r in rows}),
            "age_days": NON_MOVING_AGE_DAYS,
            "item_group": NON_MOVING_ITEM_GROUP,
        }

    # ------------------------------------------------------------------
    # Debtors
    # ------------------------------------------------------------------

    def debtors(self) -> Dict[str, Any]:
        companies = []
        for key, label, code in DEBTOR_COMPANIES:
            companies.append(
                {
                    "key": key,
                    "label": label,
                    "company_code": code,
                    "figures": self.section(
                        f"debtors_{key.lower()}", lambda c=code: self.company_debtors(c)
                    ),
                }
            )

        read = [c for c in companies if c["figures"] is not None]
        oldest = None
        for company in read:
            candidate = company["figures"]["oldest"]
            if candidate and (oldest is None or candidate["date"] < oldest["date"]):
                oldest = {**candidate, "company": company["label"]}

        total = None
        if read:
            total = {
                "amount": _money(sum(c["figures"]["amount"] for c in read)),
                "customers": sum(c["figures"]["customers"] for c in read),
                "group_amount": _money(sum(c["figures"]["group_amount"] for c in read)),
                "oldest": oldest,
                "missing": [c["label"] for c in companies if c["figures"] is None],
            }
        return {"companies": companies, "total": total, "oldest_floor": OLDEST_DEBT_FLOOR}

    def company_debtors(self, company_code: str) -> Dict[str, Any]:
        """What outside customers owe one company, and since when."""
        group_codes = INTERCOMPANY_CARD_CODES.get(company_code, [])
        reader = self._reader(company_code)

        by_kind = {row["Kind"]: row for row in reader.debtor_balances(group_codes)}
        outside = by_kind.get("C") or {}
        group = by_kind.get("G") or {}

        oldest_row = reader.oldest_debt(group_codes, OLDEST_DEBT_FLOOR)
        oldest = None
        if oldest_row is not None:
            oldest = {
                "date": _iso(oldest_row["Since"]),
                "card_code": oldest_row["CardCode"],
                "card_name": oldest_row["CardName"],
                "balance": _money(oldest_row["Balance"]),
            }

        return {
            "amount": _money(outside.get("Amount")),
            "customers": int(outside.get("Customers") or 0),
            "group_amount": _money(group.get("Amount")),
            "oldest": oldest,
        }

"""The production module's settings, one set per company.

For now these are three SAP warehouses:

* **RM warehouse** and **PM warehouse** — where the BOM draws raw and packing
  material from. That is the stock a plan counts as already at the line, and
  what a warehouse request is narrowed by, whatever warehouse a bill line names.
* **FG warehouse** — where finished goods are received.

A company that has never saved its settings runs on the defaults below, so the
module works before anybody has opened the page. Beverages starts on ``BH-PP``
because every one of its bills consumes from there; netting ``BH-PC`` off them
would ask the store for material that is already standing at the filler.
"""

from typing import Dict

from django.db import transaction

from ..models import ProductionSettings

WAREHOUSE_FIELDS = ('rm_warehouse', 'pm_warehouse', 'fg_warehouse')

DEFAULT_WAREHOUSES = {
    'rm_warehouse': 'BH-PC',
    'pm_warehouse': 'BH-PC',
    'fg_warehouse': 'BH-PF',
}

COMPANY_DEFAULT_WAREHOUSES = {
    'JIVO_BEVERAGES': {'rm_warehouse': 'BH-PP', 'pm_warehouse': 'BH-PP'},
}

WAREHOUSE_LABELS = {'rm_warehouse': 'RM', 'pm_warehouse': 'PM', 'fg_warehouse': 'FG'}


def default_warehouses(company_code: str) -> Dict[str, str]:
    return {**DEFAULT_WAREHOUSES, **COMPANY_DEFAULT_WAREHOUSES.get(company_code, {})}


def _company(company):
    """Accept a Company or its code — callers here hold either."""
    if isinstance(company, str):
        from company.models import Company
        return Company.objects.get(code=company)
    return company


def get_settings(company) -> ProductionSettings:
    """The company's settings, or an unsaved instance carrying the defaults.

    Reading never writes: the defaults are not stored until someone saves the
    page, so ``pk is None`` is how a caller tells "never changed" apart.
    """
    company = _company(company)
    found = ProductionSettings.objects.filter(company=company).first()
    if found:
        return found
    return ProductionSettings(company=company, **default_warehouses(company.code))


def _normalise(code) -> str:
    return str(code or '').strip().upper()


def active_sap_warehouses(company_code: str) -> set:
    """Every active warehouse code in the company's SAP (``OWHS``)."""
    from sap_client.client import SAPClient

    return {
        _normalise(w.warehouse_code)
        for w in SAPClient(company_code=company_code).get_active_warehouses()
    }


@transaction.atomic
def update_settings(company, data: dict, user) -> ProductionSettings:
    """Save the fields given in ``data``, after checking each against SAP.

    Only a warehouse that actually changes is checked, so an unchanged page
    saves even with SAP unreachable. A code SAP does not know is refused: every
    plan would otherwise read nothing at the line and call every component
    short. An SAP outage while checking a changed code propagates as the
    ``SAPConnectionError``/``SAPDataError`` it is, for the view to answer 503.
    """
    company = _company(company)
    current = get_settings(company)

    changes = {}
    for field in WAREHOUSE_FIELDS:
        if field not in data:
            continue
        code = _normalise(data[field])
        if not code:
            raise ValueError(f"The {WAREHOUSE_LABELS[field]} warehouse is required.")
        if code != getattr(current, field):
            changes[field] = code

    if changes:
        known = active_sap_warehouses(company.code)
        unknown = sorted({code for code in changes.values() if code not in known})
        if unknown:
            raise ValueError(
                f"Not an active warehouse in SAP: {', '.join(unknown)}."
            )

    row, _ = ProductionSettings.objects.select_for_update().get_or_create(
        company=company, defaults=default_warehouses(company.code),
    )
    for field, code in changes.items():
        setattr(row, field, code)
    row.updated_by = user
    row.save()
    return row

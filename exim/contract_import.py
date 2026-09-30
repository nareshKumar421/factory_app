"""Copy EXIM's oil contract terms: how each PO is delivered, and its freight
and brokerage per tonne.

Only the terms are copied. Everything else EXIM's contract screens held - the
vendor, oil, quantity and rate of each PO, and every truck against it - is read
live from SAP and the gate (``exim.services_contract``). EXIM kept the terms in
two places, and they are read in this order:

 1. The DC sheet (``domestic_contract_details``), one row per truck, with the
    delivery terms (FOR / EXW) and the freight and brokerage per tonne. Every
    truck of a PO carries the PO's terms; where they disagree the most common
    wins and the report says so.
 2. The contract register (``contracts``), typed PO by PO, for a PO the sheet
    does not have: its freight per tonne, and brokerage per tonne worked out
    from the brokerage amount over the tonnes loaded. It names no delivery
    terms, so a PO with freight is EXW and one without is left unset.

A re-run updates in place, and a PO whose terms were changed here since the
last copy is left alone.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .models_contract import ContractTerms, DeliveryTerms

SQL = {
    "dc": "SELECT po_number, del_terms, freight_rate, brokerage_rate FROM domestic_contract_details",
    "contracts": (
        "SELECT po_number, frieght_rate, brokerage_amount, load_qty FROM contracts WHERE deleted = 0"
    ),
}

_CENT = Decimal("0.01")


def _rows(cursor, sql):
    cursor.execute(sql)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def read_exim(cursor) -> dict:
    """Everything the copy needs from EXIM. SELECTs only."""
    return {name: _rows(cursor, sql) for name, sql in SQL.items()}


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _po(value) -> str:
    return str(value or "").strip()


@dataclass
class ImportReport:
    counts: Counter = field(default_factory=Counter)
    notes: list = field(default_factory=list)


def _from_sheet(rows) -> dict:
    by_po = defaultdict(list)
    for row in rows:
        if _po(row["po_number"]):
            by_po[_po(row["po_number"])].append(row)
    terms, disputed = {}, []
    for po, trucks in by_po.items():
        picks = {}
        for name, column in (("terms", "del_terms"), ("freight", "freight_rate"), ("brokerage", "brokerage_rate")):
            seen = Counter(
                (row[column] or "").strip().upper() if name == "terms" else _money(row[column]) for row in trucks
            )
            picks[name] = seen.most_common(1)[0][0]
            if len(seen) > 1:
                disputed.append(f"PO {po}: its trucks disagree on {name} ({dict(seen)}); kept {picks[name]}")
        delivery = picks["terms"] if picks["terms"] in DeliveryTerms.values else ""
        terms[po] = (delivery, picks["freight"], picks["brokerage"])
    return terms, disputed


def _from_register(rows) -> dict:
    terms = {}
    for row in rows:
        po = _po(row["po_number"])
        if not po:
            continue
        freight = _money(row["frieght_rate"])
        loaded = Decimal(str(row["load_qty"] or 0))
        brokerage = _money(Decimal(str(row["brokerage_amount"] or 0)) / loaded) if loaded > 0 else _money(0)
        if freight <= 0 and brokerage <= 0:
            continue
        terms[po] = (DeliveryTerms.EXW if freight > 0 else "", freight, brokerage)
    return terms


@transaction.atomic
def import_contract_terms(snapshot: dict, *, company) -> ImportReport:
    report = ImportReport()
    sheet, disputed = _from_sheet(snapshot["dc"])
    report.notes.extend(disputed)
    register = _from_register(snapshot["contracts"])
    wanted = {**register, **sheet}  # the sheet, truck by truck, over the register
    for po in sorted(set(register) & set(sheet)):
        if register[po][1] != sheet[po][1]:
            report.notes.append(
                f"PO {po}: the register says freight {register[po][1]}, the DC sheet {sheet[po][1]}; kept the sheet"
            )

    stamped = []
    existing = {t.po_number: t for t in ContractTerms.objects.filter(company=company, po_number__in=list(wanted))}
    for po, (delivery, freight, brokerage) in sorted(wanted.items()):
        values = {"delivery_terms": delivery, "freight_per_mt": freight, "brokerage_per_mt": brokerage}
        terms = existing.get(po)
        if terms is None:
            terms = ContractTerms.objects.create(company=company, po_number=po, **values)
            report.counts["create"] += 1
        elif terms.copied_from_exim_at is None or terms.updated_at > terms.copied_from_exim_at:
            report.counts["skip"] += 1
            report.notes.append(f"PO {po}: its terms were set here; left alone")
            continue
        elif any(getattr(terms, k) != v for k, v in values.items()):
            ContractTerms.objects.filter(pk=terms.pk).update(**values)
            report.counts["update"] += 1
        else:
            report.counts["unchanged"] += 1
        stamped.append(terms.pk)
    # One instant for both, at the end, so "changed here" measures from this copy.
    now = timezone.now()
    ContractTerms.objects.filter(pk__in=stamped).update(copied_from_exim_at=now, updated_at=now)
    return report

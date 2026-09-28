"""Copy EXIM's export licences into this project.

Run it to bring the register across when the Licences screen goes live, and
again as often as wanted until EXIM's own licence pages are switched off: EXIM
stays the place licences are entered until then, and each run brings the copy
back in line with it.

WHAT IT DOES
 - Reads EXIM's six licence tables (Advance headers, their import and export
   lines; DFIA the same) with SELECTs only.
 - Writes each licence into one company (Jivo Oil unless told otherwise), since
   EXIM had no companies and every licence it holds is Oil's.
 - Copies EXIM's figures AS THEY ARE, totals included, even where EXIM's own
   arithmetic had drifted from them (``discrepancies`` lists those). The one
   gap it fills is a balance EXIM never worked out, by EXIM's own rule. From
   the first line changed here, ``exim.services_licence`` recalculates.
 - Finds what an earlier run wrote by ``exim_ref``, so a re-run updates in
   place: fields that changed in EXIM are rewritten, lines EXIM removed are
   removed, lines EXIM added are added.

WHAT IT NEVER DOES
 - Write to EXIM.
 - Touch a licence changed HERE since it was copied (edited, or a line added,
   changed or removed). That licence is reported and left alone: overwriting it
   would lose somebody's work.
 - Touch a licence raised here that happens to carry an EXIM number. That is
   reported as a conflict.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .models_licence import Licence, LicenceKind, LicenceLine, LicenceStatus, LineDirection
from .services_licence import figures

ADVANCE_SQL = """
    SELECT license_no, issue_date, import_validity, export_validity,
           cif_value_inr, cif_exchange_rate, cif_value_usd,
           fob_value_inr, fob_exhange_rate, fob_value_usd,
           status, total_import_quantity, total_import, total_export, to_be_exported, balance
      FROM advance_license_headers
"""
DFIA_SQL = """
    SELECT file_no, issue_date, import_validity, export_validity,
           cif_value_inr, cif_exchange_rate, cif_value_usd,
           fob_value_inr, fob_exchange_rate, fob_value_usd,
           status, total_export_quantity, total_import, total_export, to_be_imported, balance
      FROM dfia_license_header
"""
# (table, kind, direction, SQL). Every line query returns the same seven
# columns; the last is the id of the line it is linked to, where the table has one.
# EXIM's Advance bill of entry number column is "boe_No", quoted for its capital.
LINE_QUERIES = [
    (
        "advance_license_import_lines", LicenceKind.ADVANCE, LineDirection.IMPORT,
        'SELECT id, license_no_id, "boe_No", boe_date, boe_value_usd, import_in_mts, NULL '
        "FROM advance_license_import_lines",
    ),
    (
        "advance_license_export_lines", LicenceKind.ADVANCE, LineDirection.EXPORT,
        "SELECT id, license_no_id, shipping_bill_no, sb_date, sb_value_usd, export_in_mts, "
        "linked_import_line_id FROM advance_license_export_lines",
    ),
    (
        "dfia_license_export_lines", LicenceKind.DFIA, LineDirection.EXPORT,
        "SELECT id, license_no_id, shipping_bill_no, sb_date, sb_value_usd, export_in_mts, NULL "
        "FROM dfia_license_export_lines",
    ),
    (
        "dfia_license_import_lines", LicenceKind.DFIA, LineDirection.IMPORT,
        "SELECT id, license_no_id, boe_no, boe_date, boe_value_usd, import_in_mts, "
        "linked_export_line_id FROM dfia_license_import_lines",
    ),
]
# The table a linked line id points into, for each table that links.
LINKED_TABLE = {
    "advance_license_export_lines": "advance_license_import_lines",
    "dfia_license_import_lines": "dfia_license_export_lines",
}
STATUS = {"OPEN": LicenceStatus.OPEN, "CLOSE": LicenceStatus.CLOSED, "CLOSED": LicenceStatus.CLOSED}



def _dec(value):
    """EXIM's figures as the 3-place decimals they are stored as, whatever type
    the driver hands them over in."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


class LicenceImportProblem(Exception):
    """The copy cannot run as things stand; the message says what to fix."""


def resolve_company(code):
    from company.models import Company

    company = Company.objects.filter(code=code).first()
    if company is None:
        raise LicenceImportProblem(f"There is no company {code!r} here.")
    return company


@dataclass
class EximLine:
    ref: str
    direction: str
    document_no: str
    document_date: object
    value_usd: Decimal
    quantity_mts: Decimal
    linked_ref: str | None


@dataclass
class EximLicence:
    kind: str
    number: str
    status: str
    fields: dict
    lines: list = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.number}"


@dataclass
class EximLicences:
    licences: list
    #: Lines whose licence EXIM had lost (its foreign key is SET_NULL), by table.
    orphans: dict


def _header(kind, row) -> EximLicence:
    (number, issue_date, import_validity, export_validity,
     cif_inr, cif_rate, cif_usd, fob_inr, fob_rate, fob_usd,
     status, authorised, total_import, total_export, obligation, balance) = row
    return EximLicence(
        kind=kind,
        number=str(number).strip(),
        status=(status or "").strip().upper(),
        fields={
            "issue_date": issue_date,
            "import_validity": import_validity,
            "export_validity": export_validity,
            "cif_value_inr": _dec(cif_inr),
            "cif_exchange_rate": _dec(cif_rate),
            "cif_value_usd": _dec(cif_usd),
            "fob_value_inr": _dec(fob_inr),
            "fob_exchange_rate": _dec(fob_rate),
            "fob_value_usd": _dec(fob_usd),
            "authorised_qty_mts": _dec(authorised or 0),
            "total_import_mts": _dec(total_import or 0),
            "total_export_mts": _dec(total_export or 0),
            "obligation_mts": _dec(obligation or 0),
            "balance_mts": _dec(balance),
        },
    )


def read_exim(cursor) -> EximLicences:
    """Read EXIM's licences and their lines. SELECTs only."""
    licences = {}
    for kind, sql in ((LicenceKind.ADVANCE, ADVANCE_SQL), (LicenceKind.DFIA, DFIA_SQL)):
        cursor.execute(sql)
        for row in cursor.fetchall():
            licence = _header(kind, row)
            licences[(kind, licence.number)] = licence

    orphans = defaultdict(int)
    for table, kind, direction, sql in LINE_QUERIES:
        cursor.execute(sql)
        for line_id, licence_no, doc_no, doc_date, value, qty, linked_id in cursor.fetchall():
            licence = licences.get((kind, str(licence_no).strip())) if licence_no else None
            if licence is None:
                orphans[table] += 1
                continue
            licence.lines.append(
                EximLine(
                    ref=f"{table}:{line_id}",
                    direction=direction,
                    document_no=(doc_no or "").strip(),
                    document_date=doc_date,
                    value_usd=_dec(value),
                    quantity_mts=_dec(qty),
                    linked_ref=f"{LINKED_TABLE[table]}:{linked_id}" if linked_id else None,
                )
            )
    return EximLicences(licences=list(licences.values()), orphans=dict(orphans))


@dataclass
class LicenceResult:
    ref: str
    #: create | update | unchanged | skip | conflict
    action: str
    licence_id: int | None = None
    lines_added: int = 0
    lines_updated: int = 0
    lines_removed: int = 0
    notes: list = field(default_factory=list)


@dataclass
class ImportReport:
    results: list = field(default_factory=list)
    orphans: dict = field(default_factory=dict)
    #: (ref, what EXIM stored, what its own rule gives) where the two differ.
    discrepancies: list = field(default_factory=list)

    def count(self, action):
        return sum(1 for r in self.results if r.action == action)


def _discrepancy(exim: EximLicence):
    stored = exim.fields
    obligation, _ = figures(exim.kind, stored["total_import_mts"], stored["total_export_mts"])
    if stored["obligation_mts"] != obligation:
        return (exim.ref, stored["obligation_mts"], obligation)
    return None


def _set(obj, values: dict) -> bool:
    changed = False
    for name, value in values.items():
        if getattr(obj, name) != value:
            setattr(obj, name, value)
            changed = True
    return changed


def _copy_licence(exim: EximLicence, company, result: LicenceResult):
    status = STATUS.get(exim.status)
    if status is None:
        result.action = "skip"
        result.notes.append(f"status {exim.status!r} is neither OPEN nor CLOSE")
        return

    values = {**exim.fields, "status": status}
    filled = values["balance_mts"] is None
    if filled:
        # EXIM only worked a balance out when a second-leg line was saved, so
        # a licence with none yet has no balance there. Fill it by EXIM's rule.
        second = values["total_export_mts" if exim.kind == LicenceKind.ADVANCE else "total_import_mts"]
        values["balance_mts"] = values["obligation_mts"] - second

    licence = Licence.objects.filter(exim_ref=exim.ref).first()
    if licence is None:
        clash = Licence.objects.filter(company=company, kind=exim.kind, number__iexact=exim.number)
        if clash.exists():
            result.action = "conflict"
            result.notes.append("a licence with this number was raised here; left alone")
            return
        licence = Licence(
            company=company, kind=exim.kind, number=exim.number, exim_ref=exim.ref, **values
        )
        result.action = "create"
    else:
        if licence.company_id != company.id:
            result.action = "conflict"
            result.notes.append(f"already copied into company {licence.company_id}; left alone")
            return
        # The copy stamps both with one instant, so any save here since leaves
        # updated_at ahead of it.
        if (
            licence.copied_from_exim_at is not None
            and licence.updated_at > licence.copied_from_exim_at
        ):
            result.action = "skip"
            result.licence_id = licence.id
            result.notes.append("changed here since it was copied; left alone")
            return
        filled = filled and licence.balance_mts != values["balance_mts"]
        result.action = "update" if _set(licence, values) else "unchanged"

    if filled:
        result.notes.append("balance filled in (EXIM had none)")
    licence.save()
    result.licence_id = licence.id

    existing = {line.exim_ref: line for line in licence.lines.filter(exim_ref__isnull=False)}
    by_ref = {}
    for exim_line in exim.lines:
        line_values = {
            "direction": exim_line.direction,
            "document_no": exim_line.document_no,
            "document_date": exim_line.document_date,
            "value_usd": exim_line.value_usd,
            "quantity_mts": exim_line.quantity_mts,
        }
        line = existing.pop(exim_line.ref, None)
        if line is None:
            line = LicenceLine.objects.create(licence=licence, exim_ref=exim_line.ref, **line_values)
            result.lines_added += 1
        elif _set(line, line_values):
            line.save()
            result.lines_updated += 1
        by_ref[exim_line.ref] = line

    for line in existing.values():
        line.delete()
        result.lines_removed += 1

    for exim_line in exim.lines:
        line = by_ref[exim_line.ref]
        target = by_ref.get(exim_line.linked_ref) if exim_line.linked_ref else None
        if exim_line.linked_ref and target is None:
            result.notes.append(f"{exim_line.ref} links to {exim_line.linked_ref}, not on this licence")
        if line.linked_line_id != (target.id if target else None):
            line.linked_line = target
            line.save(update_fields=["linked_line", "updated_at"])

    if result.action == "unchanged" and (
        result.lines_added or result.lines_updated or result.lines_removed
    ):
        result.action = "update"

    # Stamp both with one instant, at the END of the copy, so the "changed
    # here" test on the next run measures from when this one finished.
    stamp = timezone.now()
    Licence.objects.filter(pk=licence.pk).update(copied_from_exim_at=stamp, updated_at=stamp)


@transaction.atomic
def import_licences(snapshot: EximLicences, *, company) -> ImportReport:
    report = ImportReport(orphans=snapshot.orphans)
    for exim in sorted(snapshot.licences, key=lambda l: (l.kind, l.number)):
        result = LicenceResult(ref=exim.ref, action="unchanged")
        _copy_licence(exim, company, result)
        report.results.append(result)
        drift = _discrepancy(exim)
        if drift:
            report.discrepancies.append(drift)
    return report

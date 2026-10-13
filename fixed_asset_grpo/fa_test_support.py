"""Fixtures and SAP fakes shared by the Fixed Asset GRPO tests.

Not a test module (the name does not match ``test*.py``), so the runner does
not collect it. Every test file builds its world with ``build_fa_world``:

* company ``TC001`` (not in ``GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES``, so
  no over-receipt SAP read unless a test switches it on);
* a COMPLETED, locked FIXED_ASSET gate entry ``GE-FA-0001`` with its
  fixed-asset detail (supplier "ABD Engineering", bill "ABD/26-27/814");
* one linked PO, ``221026008`` of ``VENDA001768`` (DocEntry 14180, branch 2),
  with one line ``CG0000073`` in ``BH-FA`` -- the spec's 2.6.5 example.

Run the app's tests with:

    manage.py test fixed_asset_grpo --settings=config.sqlite_test_settings
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile

from company.models import Company, UserCompany, UserRole
from document_control.services import allocate_for_module
from driver_management.models import Driver, VehicleEntry
from fixed_asset_gatein.models import FixedAssetGateEntry
from gate_core.enums import GateEntryStatus
from gate_core.models import GateAttachment
from raw_material_gatein.models import POItemReceipt, POReceipt
from sap_client.dtos import PODTO, POItemDTO
from vehicle_management.models import Vehicle, VehicleType

User = get_user_model()

COMPANY = "TC001"
SUPPLIER = "VENDA001768"
PO_NUMBER = "221026008"
PO_DOC_ENTRY = 14180

FA_SETTINGS = {
    "FIXED_ASSET_GRPO_ENABLED": True,
    "FIXED_ASSET_GRPO_SIMULATE": False,
    "FIXED_ASSET_GRPO_ALLOW_LIVE": False,
    "FIXED_ASSET_GRPO_CUTOVER": "",
}


def make_user(email, *perms, company=None, codename_app="fixed_asset_grpo"):
    """A user with the given codenames (``app.codename`` or a bare FA codename)."""
    user = User.objects.create_user(
        email=email,
        password="pass12345",
        full_name=email.split("@")[0].title(),
        employee_code=email.split("@")[0][:20].upper(),
    )
    for perm in perms:
        app, _, codename = perm.rpartition(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app or codename_app, codename=codename)
        )
    if company is not None:
        UserCompany.objects.create(
            user=user,
            company=company,
            role=UserRole.objects.create(name=f"Role {email}"),
            is_active=True,
        )
    return user


def make_entry(cls, entry_no, *, company=None, entry_type="FIXED_ASSET",
               status=GateEntryStatus.COMPLETED, locked=True, detail=True):
    entry = VehicleEntry.objects.create(
        entry_no=entry_no,
        company=company or cls.company,
        vehicle=cls.vehicle,
        driver=cls.driver,
        entry_type=entry_type,
        status=status,
        is_locked=locked,
    )
    if detail and entry_type == "FIXED_ASSET":
        FixedAssetGateEntry.objects.create(
            vehicle_entry=entry,
            supplier_name="ABD Engineering",
            invoice_number="ABD/26-27/814",
            created_by=cls.user,
        )
    return entry


def link_po(entry, *, po_number=PO_NUMBER, warehouse="BH-FA", line=0, qty="1.000",
            item_code="CG0000073"):
    receipt = POReceipt.objects.create(
        vehicle_entry=entry,
        po_number=po_number,
        supplier_code=SUPPLIER,
        supplier_name="ABD Engineering",
        sap_doc_entry=PO_DOC_ENTRY,
        branch_id=2,
        vendor_ref="",
        po_date=date(2026, 10, 1),
    )
    add_line(receipt, line=line, warehouse=warehouse, qty=qty, item_code=item_code)
    return receipt


def add_line(receipt, *, line, warehouse="BH-FA", qty="1.000", item_code="CG0000073"):
    return POItemReceipt.objects.create(
        po_receipt=receipt,
        po_item_code=item_code,
        item_name="Weighing scale",
        ordered_qty=Decimal(qty),
        received_qty=Decimal(qty),
        accepted_qty=Decimal(qty),
        rejected_qty=Decimal("0"),
        sap_line_num=line,
        unit_price=Decimal("1270.000000"),
        tax_code="GST18",
        warehouse_code=warehouse,
        gl_account="1204023",
        variety="CANOLA",
        uom="NOS",
    )


def gate_photo(entry, name="invoice.jpg", *, active=True):
    return GateAttachment.objects.create(
        gate_entry=entry,
        file=SimpleUploadedFile(name, b"jpeg-bytes", content_type="image/jpeg"),
        is_active=active,
        document_code=allocate_for_module("GATE"),
    )


def build_fa_world(cls):
    """The shared fixtures (see the module docstring), set on ``cls``."""
    cls.company = Company.objects.create(name="Test Company", code=COMPANY)
    cls.other_company = Company.objects.create(name="Other Company", code="TC002")
    cls.user = User.objects.create_user(
        email="fixture@example.com", password="pass12345",
        full_name="Fixture User", employee_code="FIX001",
    )
    vehicle_type = VehicleType.objects.create(name="TRUCK")
    cls.vehicle = Vehicle.objects.create(vehicle_number="HR26FA0001", vehicle_type=vehicle_type)
    cls.driver = Driver.objects.create(name="FA Driver", mobile_no="9876500001", license_no="DLFA0001")
    cls.entry = make_entry(cls, "GE-FA-0001")
    cls.po_receipt = link_po(cls.entry)
    cls.po_item = cls.po_receipt.items.get()


def grpo_payload(test, **overrides):
    """A JSON-native FA GRPO request, as the screen sends it."""
    payload = {
        "vehicle_entry_id": test.entry.id,
        "po_receipt_ids": [test.po_receipt.id],
        "items": [{
            "po_item_receipt_id": test.po_item.id,
            "accepted_qty": "1.000",
            "unit_price": "1270.0",
            "tax_code": "GST18",
            "gl_account": "1204023",
        }],
        "branch_id": 2,
        "vendor_ref": "ABD/26-27/814",
        "doc_date": "2026-10-03",
        "doc_due_date": "2026-10-03",
        "tax_date": "2026-10-03",
    }
    payload.update(overrides)
    return payload


def open_po(*, remaining=5.0, warehouse="BH-FA", lines=None, po_number=PO_NUMBER):
    """An open fixed-asset PO as ``sap_purchasing.fa_po_reader`` returns it."""
    items = lines or [
        POItemDTO(
            po_item_code="CG0000073", item_name="Weighing scale", ordered_qty=5.0,
            received_qty=5.0 - remaining, remaining_qty=remaining, uom="NOS", rate=1270.0,
            line_num=0, tax_code="GST18", warehouse_code=warehouse, account_code="1204023",
            variety="CANOLA",
        )
    ]
    return PODTO(
        po_number=po_number, supplier_code=SUPPLIER, supplier_name="ABD Engineering " + "X" * 200,
        items=items, doc_entry=PO_DOC_ENTRY, branch_id=2, vendor_ref="PO-REF", doc_date=date(2026, 10, 1),
    )


def link_body(*, po_number=PO_NUMBER, qty="1.000", line=0, item_code="CG0000073", **extra):
    body = {
        "po_number": po_number,
        "supplier_code": SUPPLIER,
        "supplier_name": "ABD Engineering",
        "items": [{
            "line_num": line, "po_item_code": item_code, "item_name": "Weighing scale",
            "received_qty": qty, "uom": "NOS",
        }],
    }
    body.update(extra)
    return body


def fake_sap():
    """A stand-in for ``grpo.services.SAPClient(...)``: SAP answers, nothing is real."""
    sap = MagicMock()
    sap.upload_attachment.return_value = {"AbsoluteEntry": 4567}
    sap.create_grpo.return_value = {"DocEntry": 9001, "DocNum": 30001, "DocTotal": 1498.6}
    sap.batch_managed_flags.return_value = {}
    sap.get_po_open_qtys.return_value = {(PO_DOC_ENTRY, 0): 1000.0}
    return sap


def company_context(db_name):
    """What ``sap_purchasing.live_guard.CompanyContext(code)`` returns, for one database."""
    return MagicMock(return_value=SimpleNamespace(
        service_layer={"company_db": db_name}, hana={"schema": db_name},
    ))

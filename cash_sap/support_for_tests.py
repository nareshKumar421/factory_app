"""Shared fixture for the cash_sap tests (not itself a test module).

Builds one cash book company coded ``JIVO_OIL`` (a key of the SAP registry,
so it is the "home" company), its five branches routed as migration 0002 seeds
them, a small G/L item map, and the people: a poster, a settings user, a
reader and a custodian.

SAP is never reached. The live guard reads ``CompanyContext``, which every
test that needs a decision patches at ``sap_purchasing.live_guard.CompanyContext``
(:func:`patch_guard`). The handlers' SAP client is patched at
``cash_sap.sap_posting.PurchasingClient``.

Run the app's tests with::

    python manage.py test cash_sap --settings=config.sqlite_test_settings
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from cash_book.models import (
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
    CashEntryAttachment,
    EntryApprovalStatus,
)
from company.models import Company, UserCompany, UserRole

from .models import CashBranchSapRouting, GLItemMap, VoucherSapInfo

User = get_user_model()

#: Flags every cash_sap test sets explicitly: the defaults follow DEBUG.
SIMULATE_FLAGS = dict(CASH_SAP_ENABLED=True, CASH_SAP_SIMULATE=True, CASH_SAP_ALLOW_LIVE=False,
                      CASH_SAP_SEND_SERIES=True, CASH_SAP_UPPERCASE_COMMENTS=False)
REAL_FLAGS = dict(SIMULATE_FLAGS, CASH_SAP_SIMULATE=False)

simulate_settings = override_settings(**SIMULATE_FLAGS)
real_settings = override_settings(**REAL_FLAGS)


def fake_context(*, test=True):
    """A ``CompanyContext`` stand-in: TEST_ databases, or live ones."""
    db = "TEST_JIVO_OIL" if test else "JIVO_OIL_HANADB"
    return MagicMock(return_value=SimpleNamespace(
        service_layer={"company_db": db}, hana={"schema": db}))


def patch_guard(testcase, *, test=True):
    patcher = patch("sap_purchasing.live_guard.CompanyContext", fake_context(test=test))
    patcher.start()
    testcase.addCleanup(patcher.stop)


def grant(user, *codes):
    for code in codes:
        app_label, codename = code.split(".", 1)
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename))


class CashSapFixture(TestCase):
    """One company, five branches, routing, a G/L map, and four people."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.role, _ = UserRole.objects.get_or_create(name="Accounts")

        cls.oil = CashBranch.objects.create(company=cls.company, name="Oil", sort_order=0)
        cls.beverage = CashBranch.objects.create(company=cls.company, name="Beverage", sort_order=1)
        cls.water = CashBranch.objects.create(company=cls.company, name="Water", sort_order=2)
        cls.common = CashBranch.objects.create(company=cls.company, name="Common", sort_order=3)
        cls.mart = CashBranch.objects.create(company=cls.company, name="Mart", sort_order=4)

        oil_routing = dict(
            enabled=True, sap_company_code="JIVO_OIL", mode="PO_GRPO", vendor_code="ORGV000465",
            bpl_id=2, warehouse_code="BH-FA", ocr_code="CANOLA", ocr_code3="Factory", ocr_code5="HR",
            item_tax_code="IGST@0", service_tax_code="Exampt", ap_drafts_enabled=False,
            grpo_needs_attachment=True, grpo_grouping="NONE",
        )
        cls.oil_routing = CashBranchSapRouting.objects.create(branch=cls.oil, **oil_routing)
        cls.common_routing = CashBranchSapRouting.objects.create(branch=cls.common, **oil_routing)
        bev = dict(enabled=True, sap_company_code="JIVO_BEVERAGES", mode="AP_DRAFT_ONLY",
                   vendor_code="ORGV000245", bpl_id=None, ocr_code3="Factory")
        cls.beverage_routing = CashBranchSapRouting.objects.create(branch=cls.beverage, **bev)
        cls.water_routing = CashBranchSapRouting.objects.create(branch=cls.water, **bev)
        cls.mart_routing = CashBranchSapRouting.objects.create(branch=cls.mart, enabled=False)

        for gl, name, item, review in (
            ("5630004", "REFRESHMENT", "CG0000015", False),
            ("5680015", "HOUSE KEEPING", "CG0000005", False),
            ("5680012", "PRINTING AND STATIONERY", "CG0000013", True),
        ):
            GLItemMap.objects.create(sap_company_code="JIVO_OIL", gl_account_code=gl,
                                     gl_account_name=name, treatment="ITEM", item_code=item,
                                     needs_review=review)
        for gl, name in (("5690002", "CONVEYANCE"), ("5680023", "POSTAGE & COURIER")):
            GLItemMap.objects.create(sap_company_code="JIVO_OIL", gl_account_code=gl,
                                     gl_account_name=name, treatment="SERVICE")
        GLItemMap.objects.create(sap_company_code="JIVO_OIL", gl_account_code="5660099",
                                 gl_account_name="DONATION", treatment="NEVER")

        cls.poster = cls._person("poster@example.com", "Vishal Tyagi",
                                 "cash_book.can_view_cash_book", "cash_sap.can_post_cash_to_sap")
        cls.settings_user = cls._person("settings@example.com", "Settings Person",
                                        "cash_book.can_view_cash_book",
                                        "cash_sap.can_manage_cash_sap_settings")
        cls.reader = cls._person("reader@example.com", "Reader", "cash_book.can_view_cash_book")
        cls.custodian = cls._person("custodian@example.com", "Custodian",
                                    "cash_book.can_view_cash_book", "cash_book.can_manage_cash_book")
        cls._serial = 100
        cls._bunch_number = 0

    @classmethod
    def _person(cls, email, name, *codes):
        user = User.objects.create(email=email, full_name=name)
        grant(user, *codes)
        UserCompany.objects.create(user=user, company=cls.company, role=cls.role)
        return user

    # -- builders ----------------------------------------------------------------

    def voucher(self, amount="350.00", *, gl="5630004", gl_name="REFRESHMENT", branch=None,
                entry_date=date(2026, 9, 3), bill=True, proof=False, approved=True,
                direction=CashDirection.OUT, item="Tea", detail="Tea for the shift",
                bill_date=None, gate_entry_no="", supplier_bill_no="", company=None):
        type(self)._serial += 1
        entry = CashEntry.objects.create(
            company=company or self.company,
            serial_number=type(self)._serial,
            entry_date=entry_date,
            direction=direction,
            amount=Decimal(amount),
            branch=(branch or self.oil) if direction == CashDirection.OUT else None,
            gl_account_code=gl if direction == CashDirection.OUT else "",
            gl_account_name=gl_name if direction == CashDirection.OUT else "",
            item=item,
            detail=detail,
            approval_state=(EntryApprovalStatus.APPROVED if approved else EntryApprovalStatus.PENDING)
            if direction == CashDirection.OUT else EntryApprovalStatus.NOT_REQUIRED,
        )
        if bill or proof:
            CashEntryAttachment.objects.create(
                entry=entry,
                file=SimpleUploadedFile(f"bill-{entry.id}.jpg", b"jpeg", content_type="image/jpeg"),
                is_approval_proof=proof and not bill,
                original_filename=f"bill-{entry.id}.jpg",
                size_bytes=4,
            )
        if bill_date or gate_entry_no or supplier_bill_no:
            VoucherSapInfo.objects.create(entry=entry, bill_date=bill_date,
                                          gate_entry_no=gate_entry_no,
                                          supplier_bill_no=supplier_bill_no)
        return entry

    def bunch(self, *entries, company=None):
        type(self)._bunch_number += 1
        bunch = CashBunch.objects.create(company=company or self.company,
                                         number=type(self)._bunch_number)
        CashEntry.objects.filter(pk__in=[e.pk for e in entries]).update(bunch=bunch)
        return bunch


TODAY = date(2026, 10, 9)

from django.urls import path

from .views import (
    AdvanceEntryDetailAPI,
    AdvanceEntryListCreateAPI,
    AdvanceHolderListAPI,
    AdvanceStatementAPI,
    AtmAccountDetailAPI,
    AtmAccountListCreateAPI,
    AtmReceiptCreateAPI,
    AtmReceiptDetailAPI,
    CashApprovalQueueAPI,
    CashApproversAPI,
    CashBookOptionsAPI,
    CashBranchDetailAPI,
    CashBranchListCreateAPI,
    CashBookSummaryAPI,
    CashBunchDetailAPI,
    CashBunchExportAPI,
    CashBunchListCreateAPI,
    CashBunchSentAPI,
    CashEntryApprovalDecideAPI,
    CashEntryApproveOnPaperAPI,
    CashEntryAttachmentAPI,
    CashEntryAttachmentDetailAPI,
    CashEntryBunchRemoveAPI,
    CashEntryColumnValuesAPI,
    CashEntryDetailAPI,
    CashEntryListCreateAPI,
    CashPeopleAPI,
    CashPersonCreateAPI,
    GLAccountSearchAPI,
    SalaryAdvanceDecideAPI,
    SalaryAdvanceDeductedAPI,
    SalaryAdvanceDetailAPI,
    SalaryAdvanceEmployeesAPI,
    SalaryAdvanceListCreateAPI,
)

urlpatterns = [
    path("options/", CashBookOptionsAPI.as_view(), name="cash-book-options"),
    path("summary/", CashBookSummaryAPI.as_view(), name="cash-book-summary"),
    path("gl-accounts/", GLAccountSearchAPI.as_view(), name="cash-book-gl-accounts"),
    # The branch list behind the entry form, and its settings screen.
    path("branches/", CashBranchListCreateAPI.as_view(), name="cash-book-branches"),
    path(
        "branches/<int:pk>/",
        CashBranchDetailAPI.as_view(),
        name="cash-book-branch-detail",
    ),
    # The card the cash is drawn off. A withdrawal is not a row here -- it is
    # the cash receipt it produces, so it is recorded on the entry itself.
    path("atm/", AtmAccountListCreateAPI.as_view(), name="cash-book-atm"),
    path("atm/<int:pk>/", AtmAccountDetailAPI.as_view(), name="cash-book-atm-detail"),
    path(
        "atm/<int:pk>/receipts/",
        AtmReceiptCreateAPI.as_view(),
        name="cash-book-atm-receipts",
    ),
    path(
        "atm/receipts/<int:pk>/",
        AtmReceiptDetailAPI.as_view(),
        name="cash-book-atm-receipt-detail",
    ),
    # Cash handed to somebody who has not yet said what it went on.
    path("advances/", AdvanceEntryListCreateAPI.as_view(), name="cash-book-advances"),
    path(
        "advances/<int:pk>/",
        AdvanceEntryDetailAPI.as_view(),
        name="cash-book-advance-detail",
    ),
    path(
        "advances/holders/",
        AdvanceHolderListAPI.as_view(),
        name="cash-book-advance-holders",
    ),
    path(
        "advances/holders/<int:pk>/",
        AdvanceStatementAPI.as_view(),
        name="cash-book-advance-statement",
    ),
    # Cash given against a wage, which comes back off it. A different thing
    # from a float: this money became theirs, and HR decide how it returns.
    path(
        "salary-advances/",
        SalaryAdvanceListCreateAPI.as_view(),
        name="cash-book-salary-advances",
    ),
    # Before the detail route, so "decide" and "employees" are never read as
    # an id -- they are words, and an int converter would 404 on them anyway,
    # but the order is what makes that a certainty rather than a coincidence.
    path(
        "salary-advances/decide/",
        SalaryAdvanceDecideAPI.as_view(),
        name="cash-book-salary-advances-decide",
    ),
    path(
        "salary-advances/employees/",
        SalaryAdvanceEmployeesAPI.as_view(),
        name="cash-book-salary-advance-employees",
    ),
    path(
        "salary-advances/<int:pk>/",
        SalaryAdvanceDetailAPI.as_view(),
        name="cash-book-salary-advance-detail",
    ),
    path(
        "salary-advances/<int:pk>/deducted/",
        SalaryAdvanceDeductedAPI.as_view(),
        name="cash-book-salary-advance-deducted",
    ),
    path("people/", CashPeopleAPI.as_view(), name="cash-book-people"),
    path(
        "people/new/",
        CashPersonCreateAPI.as_view(),
        name="cash-book-person-create",
    ),
    # The register itself.
    path("entries/", CashEntryListCreateAPI.as_view(), name="cash-book-entries"),
    path(
        "entries/columns/",
        CashEntryColumnValuesAPI.as_view(),
        name="cash-book-entry-columns",
    ),
    path(
        "entries/<int:pk>/",
        CashEntryDetailAPI.as_view(),
        name="cash-book-entry-detail",
    ),
    # The bill behind a line: photographs and PDFs of the voucher.
    path(
        "entries/<int:pk>/attachments/",
        CashEntryAttachmentAPI.as_view(),
        name="cash-book-entry-attachments",
    ),
    path(
        "attachments/<int:pk>/",
        CashEntryAttachmentDetailAPI.as_view(),
        name="cash-book-attachment-detail",
    ),
    # Approval belongs to the entry, and a payment joins the queue the moment
    # it is recorded -- so there is nothing to send, only to decide.
    path(
        "entries/decide/",
        CashEntryApprovalDecideAPI.as_view(),
        name="cash-book-entries-decide",
    ),
    # The other way in: the custodian recording a signature they already have.
    path(
        "entries/approve-on-paper/",
        CashEntryApproveOnPaperAPI.as_view(),
        name="cash-book-entries-approve-on-paper",
    ),
    path("approvals/", CashApprovalQueueAPI.as_view(), name="cash-book-approvals"),
    path("approvers/", CashApproversAPI.as_view(), name="cash-book-approvers"),
    # The paper batch: approved vouchers bundled, downloaded and mailed.
    path("bunches/", CashBunchListCreateAPI.as_view(), name="cash-book-bunches"),
    path(
        "bunches/<int:pk>/",
        CashBunchDetailAPI.as_view(),
        name="cash-book-bunch-detail",
    ),
    path(
        "bunches/<int:pk>/export/",
        CashBunchExportAPI.as_view(),
        name="cash-book-bunch-export",
    ),
    path(
        "bunches/<int:pk>/sent/",
        CashBunchSentAPI.as_view(),
        name="cash-book-bunch-sent",
    ),
    path(
        "entries/<int:pk>/bunch/",
        CashEntryBunchRemoveAPI.as_view(),
        name="cash-book-entry-bunch-remove",
    ),
]

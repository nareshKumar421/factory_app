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
    CashBookOptionsAPI,
    CashBranchDetailAPI,
    CashBranchListCreateAPI,
    CashBookSummaryAPI,
    CashBunchApproveAPI,
    CashBunchDetailAPI,
    CashBunchListCreateAPI,
    CashBunchRejectAPI,
    CashBunchResendAPI,
    CashEntryApprovalDecideAPI,
    CashEntryApprovalSendAPI,
    CashEntryDetailAPI,
    CashEntryListCreateAPI,
    CashPeopleAPI,
    GLAccountSearchAPI,
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
    path("people/", CashPeopleAPI.as_view(), name="cash-book-people"),
    # The register itself.
    path("entries/", CashEntryListCreateAPI.as_view(), name="cash-book-entries"),
    path(
        "entries/<int:pk>/",
        CashEntryDetailAPI.as_view(),
        name="cash-book-entry-detail",
    ),
    # Approval belongs to the entry. A bunch is the bundle of paper it was
    # carried over in, and no longer decides anything.
    path(
        "entries/send-for-approval/",
        CashEntryApprovalSendAPI.as_view(),
        name="cash-book-entries-send",
    ),
    path(
        "entries/decide/",
        CashEntryApprovalDecideAPI.as_view(),
        name="cash-book-entries-decide",
    ),
    path("approvals/", CashApprovalQueueAPI.as_view(), name="cash-book-approvals"),
    # The sheet's Bunch column: vouchers bundled and walked over together.
    path("bunches/", CashBunchListCreateAPI.as_view(), name="cash-book-bunches"),
    path(
        "bunches/<int:pk>/",
        CashBunchDetailAPI.as_view(),
        name="cash-book-bunch-detail",
    ),
    path(
        "bunches/<int:pk>/approve/",
        CashBunchApproveAPI.as_view(),
        name="cash-book-bunch-approve",
    ),
    path(
        "bunches/<int:pk>/reject/",
        CashBunchRejectAPI.as_view(),
        name="cash-book-bunch-reject",
    ),
    path(
        "bunches/<int:pk>/resend/",
        CashBunchResendAPI.as_view(),
        name="cash-book-bunch-resend",
    ),
]

from django.urls import path

from .views import (
    CashBookOptionsAPI,
    CashBranchDetailAPI,
    CashBranchListCreateAPI,
    CashBookSummaryAPI,
    CashBunchApproveAPI,
    CashBunchDetailAPI,
    CashBunchListCreateAPI,
    CashBunchRejectAPI,
    CashBunchResendAPI,
    CashEntryDetailAPI,
    CashEntryListCreateAPI,
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
    # The register itself.
    path("entries/", CashEntryListCreateAPI.as_view(), name="cash-book-entries"),
    path(
        "entries/<int:pk>/",
        CashEntryDetailAPI.as_view(),
        name="cash-book-entry-detail",
    ),
    # The other half of the sheet's Bunch column: vouchers sent for approval.
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

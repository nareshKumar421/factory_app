from django.urls import path

from .views import (
    InvoiceApprovalAuditView,
    InvoiceApprovalHistoryView,
    InvoiceApprovalListView,
    InvoiceApprovalPendingCountView,
    InvoiceApprovalStatusUpdateView,
)

urlpatterns = [
    path("invoices/", InvoiceApprovalListView.as_view(), name="invoice-approval-list"),
    path(
        "invoices/pending-count/",
        InvoiceApprovalPendingCountView.as_view(),
        name="invoice-approval-pending-count",
    ),
    path(
        "invoices/<int:pk>/status/",
        InvoiceApprovalStatusUpdateView.as_view(),
        name="invoice-approval-status",
    ),
    path(
        "invoices/<int:pk>/history/",
        InvoiceApprovalHistoryView.as_view(),
        name="invoice-approval-history",
    ),
    path(
        "invoices/<int:pk>/audit/",
        InvoiceApprovalAuditView.as_view(),
        name="invoice-approval-audit",
    ),
]

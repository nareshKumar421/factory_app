from django.urls import path

from .views import (
    InvoiceApprovalAuditView,
    InvoiceApprovalHistoryView,
    InvoiceApprovalListView,
    InvoiceApprovalPendingCountView,
    InvoiceApprovalStatusUpdateView,
    OmsInvoiceAuditView,
    OmsInvoiceHistoryView,
    OmsInvoiceListView,
    OmsInvoicePendingCountView,
    OmsInvoiceStatusUpdateView,
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
    # OMS invoices (external OMS service — the page's default source)
    path(
        "oms-invoices/",
        OmsInvoiceListView.as_view(),
        name="oms-invoice-approval-list",
    ),
    path(
        "oms-invoices/pending-count/",
        OmsInvoicePendingCountView.as_view(),
        name="oms-invoice-approval-pending-count",
    ),
    path(
        "oms-invoices/<int:pk>/status/",
        OmsInvoiceStatusUpdateView.as_view(),
        name="oms-invoice-approval-status",
    ),
    path(
        "oms-invoices/<int:pk>/history/",
        OmsInvoiceHistoryView.as_view(),
        name="oms-invoice-approval-history",
    ),
    path(
        "oms-invoices/<int:pk>/audit/",
        OmsInvoiceAuditView.as_view(),
        name="oms-invoice-approval-audit",
    ),
]

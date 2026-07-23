from django.urls import path

from .views import (
    OmsInvoiceAuditView,
    OmsInvoiceHistoryView,
    OmsInvoiceListView,
    OmsInvoiceStatusUpdateView,
    OmsPendingCountView,
)

urlpatterns = [
    path("invoices/", OmsInvoiceListView.as_view(), name="oms-invoice-list"),
    path("invoices/pending-count/", OmsPendingCountView.as_view(), name="oms-invoice-pending-count"),
    path("invoices/<int:pk>/status/", OmsInvoiceStatusUpdateView.as_view(), name="oms-invoice-status"),
    path("invoices/<int:pk>/history/", OmsInvoiceHistoryView.as_view(), name="oms-invoice-history"),
    path("invoices/<int:pk>/audit/", OmsInvoiceAuditView.as_view(), name="oms-invoice-audit"),
]

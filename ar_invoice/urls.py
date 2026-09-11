from django.urls import path

from .views import (
    ARCashSaleHistoryView,
    ARInvoiceCancelView,
    ARInvoiceDetailView,
    ARInvoiceListCreateView,
    ARInvoicePostDraftView,
    ARInvoicePostView,
    ARInvoicePrintView,
    ARInvoiceRefreshView,
    CustomerCreditView,
    CustomerSearchView,
    LineDefaultsView,
    OpenSOLinesView,
    WarehouseItemsView,
)

urlpatterns = [
    path("customers/", CustomerSearchView.as_view(), name="ar-invoice-customers"),
    path(
        "customer-credit/",
        CustomerCreditView.as_view(),
        name="ar-invoice-customer-credit",
    ),
    path("open-so-lines/", OpenSOLinesView.as_view(), name="ar-invoice-open-so-lines"),
    path("items/", WarehouseItemsView.as_view(), name="ar-invoice-items"),
    path("line-defaults/", LineDefaultsView.as_view(), name="ar-invoice-line-defaults"),
    path("invoices/", ARInvoiceListCreateView.as_view(), name="ar-invoice-list-create"),
    path(
        "sap-invoices/",
        ARCashSaleHistoryView.as_view(),
        name="ar-invoice-sap-cash-sales",
    ),
    path("invoices/<int:pk>/", ARInvoiceDetailView.as_view(), name="ar-invoice-detail"),
    path("invoices/<int:pk>/post/", ARInvoicePostView.as_view(), name="ar-invoice-post"),
    path("invoices/<int:pk>/refresh/", ARInvoiceRefreshView.as_view(), name="ar-invoice-refresh"),
    path("invoices/<int:pk>/print/", ARInvoicePrintView.as_view(), name="ar-invoice-print"),
    path(
        "invoices/<int:pk>/post-draft/",
        ARInvoicePostDraftView.as_view(),
        name="ar-invoice-post-draft",
    ),
    path(
        "invoices/<int:pk>/cancel/",
        ARInvoiceCancelView.as_view(),
        name="ar-invoice-cancel",
    ),
]

from django.urls import path

from .views_bill_summary import (
    BillSummaryApproveAPI,
    BillSummaryBulkSubmitAPI,
    BillSummaryCancelAPI,
    BillSummaryDetailAPI,
    BillSummaryInvoicePrintAPI,
    BillSummaryListCreateAPI,
    BillSummaryPickAPI,
    BillSummaryPrintedAPI,
    BillSummaryLookupAPI,
    BillSummaryRejectAPI,
    BillSummaryResubmitAPI,
    BillSummarySapAdoptAPI,
    BillSummarySapDetailAPI,
    BillSummarySapListAPI,
    BillSummaryStampAPI,
)

from .views_freight_rate import FreightRateAPI
from .views_freight_approval import (
    DispatchFreightApprovalListAPI,
    DispatchFreightApproveAPI,
    DispatchFreightRejectAPI,
    TruckFreightAPI,
    TruckFreightBoardAPI,
)
from .views_freight_benchmark import (
    FreightBenchmarkTableAPI,
    FreightDestinationCreateAPI,
    FreightDestinationDetailAPI,
    FreightSlabCreateAPI,
    FreightSlabDetailAPI,
)
from .views_transporter_account import TransporterAccountAPI

from .views import (
    DispatchBiltyAttachmentAPI,
    DispatchBiltyGRPOOptionsAPI,
    DispatchBiltyGRPOPostingDetailAPI,
    DispatchBiltyGRPOPostingHistoryAPI,
    DispatchBiltyGRPOSummaryAPI,
    DispatchBiltyGRPOPreviewAPI,
    DispatchBiltyServiceGRPOPostAPI,
    DispatchPendingBiltyGRPOListAPI,
    OpenBiltyListAPI,
    TransporterAPInvoiceDetailAPI,
    TransporterAPInvoiceHistoryAPI,
    TransporterAPInvoicePostAPI,
    TransporterAPInvoicePreviewAPI,
    TransporterAPInvoiceSubmitAPI,
)

urlpatterns = [
    path("open-bilties/", OpenBiltyListAPI.as_view(), name="dispatch-open-bilties"),
    # What a litre cost to move: freight from SAP, litres from the GRPO lines.
    path(
        "freight-rate/",
        FreightRateAPI.as_view(),
        name="dispatch-freight-rate",
    ),
    # What a truckload should cost to each destination, by vehicle size: the
    # benchmark a vehicle's actual freight is held against.
    path(
        "freight-benchmarks/",
        FreightBenchmarkTableAPI.as_view(),
        name="dispatch-freight-benchmarks",
    ),
    path(
        "freight-benchmarks/destinations/",
        FreightDestinationCreateAPI.as_view(),
        name="dispatch-freight-destination-create",
    ),
    path(
        "freight-benchmarks/destinations/<int:pk>/",
        FreightDestinationDetailAPI.as_view(),
        name="dispatch-freight-destination-detail",
    ),
    path(
        "freight-benchmarks/slabs/",
        FreightSlabCreateAPI.as_view(),
        name="dispatch-freight-slab-create",
    ),
    path(
        "freight-benchmarks/slabs/<int:pk>/",
        FreightSlabDetailAPI.as_view(),
        name="dispatch-freight-slab-detail",
    ),
    # Trucks linked at a freight over their benchmark, and the decision on them.
    # The request is raised by entering the truck's freight at Vehicle Linking.
    path(
        "freight-approvals/",
        DispatchFreightApprovalListAPI.as_view(),
        name="dispatch-freight-approvals",
    ),
    path(
        "freight-approvals/truck/",
        TruckFreightAPI.as_view(),
        name="dispatch-freight-approval-truck",
    ),
    path(
        "freight-approvals/trucks/",
        TruckFreightBoardAPI.as_view(),
        name="dispatch-freight-approval-trucks",
    ),
    path(
        "freight-approvals/<int:pk>/approve/",
        DispatchFreightApproveAPI.as_view(),
        name="dispatch-freight-approval-approve",
    ),
    path(
        "freight-approvals/<int:pk>/reject/",
        DispatchFreightRejectAPI.as_view(),
        name="dispatch-freight-approval-reject",
    ),
    path(
        "transporter-account/",
        TransporterAccountAPI.as_view(),
        name="dispatch-transporter-account",
    ),
    path(
        "bilty-grpo/pending/",
        DispatchPendingBiltyGRPOListAPI.as_view(),
        name="dispatch-bilty-grpo-pending",
    ),
    path(
        "bilty-grpo/options/",
        DispatchBiltyGRPOOptionsAPI.as_view(),
        name="dispatch-bilty-grpo-options",
    ),
    path(
        "bilty-grpo/preview/<int:dispatch_plan_id>/",
        DispatchBiltyGRPOPreviewAPI.as_view(),
        name="dispatch-bilty-grpo-preview",
    ),
    path(
        "bilty-grpo/attachment/<int:dispatch_plan_id>/",
        DispatchBiltyAttachmentAPI.as_view(),
        name="dispatch-bilty-grpo-attachment",
    ),
    path(
        "bilty-grpo/post/",
        DispatchBiltyServiceGRPOPostAPI.as_view(),
        name="dispatch-bilty-grpo-post",
    ),
    path(
        "bilty-grpo/summary/",
        DispatchBiltyGRPOSummaryAPI.as_view(),
        name="dispatch-bilty-grpo-summary",
    ),
    path(
        "bilty-grpo/history/",
        DispatchBiltyGRPOPostingHistoryAPI.as_view(),
        name="dispatch-bilty-grpo-history",
    ),
    path(
        "bilty-grpo/<int:posting_id>/",
        DispatchBiltyGRPOPostingDetailAPI.as_view(),
        name="dispatch-bilty-grpo-detail",
    ),
    path(
        "transporter-invoices/preview/",
        TransporterAPInvoicePreviewAPI.as_view(),
        name="dispatch-transporter-invoice-preview",
    ),
    path(
        "transporter-invoices/submit/",
        TransporterAPInvoiceSubmitAPI.as_view(),
        name="dispatch-transporter-invoice-submit",
    ),
    path(
        "transporter-invoices/post-ap-invoice/",
        TransporterAPInvoicePostAPI.as_view(),
        name="dispatch-transporter-invoice-post",
    ),
    path(
        "transporter-invoices/<int:posting_id>/post-ap-invoice/",
        TransporterAPInvoicePostAPI.as_view(),
        name="dispatch-transporter-invoice-post-submitted",
    ),
    path(
        "transporter-invoices/history/",
        TransporterAPInvoiceHistoryAPI.as_view(),
        name="dispatch-transporter-invoice-history",
    ),
    path(
        "transporter-invoices/<int:posting_id>/",
        TransporterAPInvoiceDetailAPI.as_view(),
        name="dispatch-transporter-invoice-detail",
    ),

    # ------------------------------------------------------------------
    # Bill summary — the picking sheet the floor works from.
    # ------------------------------------------------------------------
    path(
        "bill-summaries/lookup/",
        BillSummaryLookupAPI.as_view(),
        name="bill-summary-lookup",
    ),
    path(
        "bill-summaries/",
        BillSummaryListCreateAPI.as_view(),
        name="bill-summary-list-create",
    ),
    # A whole truck at once, and the warehouse's decision over a whole truck at
    # once. Both before the <int:pk> route so neither word is read as a sheet id.
    path(
        "bill-summaries/bulk/",
        BillSummaryBulkSubmitAPI.as_view(),
        name="bill-summary-bulk-submit",
    ),
    path(
        "bill-summaries/approve/",
        BillSummaryApproveAPI.as_view(),
        name="bill-summary-approve",
    ),
    # Dispatches stamped straight into SAP. Before the <int:pk> route, so
    # "sap" is never read as a sheet id.
    path(
        "bill-summaries/sap/",
        BillSummarySapListAPI.as_view(),
        name="bill-summary-sap-list",
    ),
    path(
        "bill-summaries/sap/<int:doc_entry>/",
        BillSummarySapDetailAPI.as_view(),
        name="bill-summary-sap-detail",
    ),
    path(
        "bill-summaries/sap/<int:doc_entry>/adopt/",
        BillSummarySapAdoptAPI.as_view(),
        name="bill-summary-sap-adopt",
    ),
    # The BILL itself, keyed by the invoice rather than by a sheet: a dispatch
    # stamped straight into SAP has no sheet id to ask with. Before the
    # <int:pk> route for the same reason "sap" is.
    path(
        "bill-summaries/invoice/<int:doc_entry>/print/",
        BillSummaryInvoicePrintAPI.as_view(),
        name="bill-summary-invoice-print",
    ),
    path(
        "bill-summaries/<int:pk>/",
        BillSummaryDetailAPI.as_view(),
        name="bill-summary-detail",
    ),
    path(
        "bill-summaries/<int:pk>/reject/",
        BillSummaryRejectAPI.as_view(),
        name="bill-summary-reject",
    ),
    path(
        "bill-summaries/<int:pk>/resubmit/",
        BillSummaryResubmitAPI.as_view(),
        name="bill-summary-resubmit",
    ),
    path(
        "bill-summaries/<int:pk>/printed/",
        BillSummaryPrintedAPI.as_view(),
        name="bill-summary-printed",
    ),
    path(
        "bill-summaries/<int:pk>/pick/",
        BillSummaryPickAPI.as_view(),
        name="bill-summary-pick",
    ),
    path(
        "bill-summaries/<int:pk>/stamp-sap/",
        BillSummaryStampAPI.as_view(),
        name="bill-summary-stamp-sap",
    ),
    path(
        "bill-summaries/<int:pk>/cancel/",
        BillSummaryCancelAPI.as_view(),
        name="bill-summary-cancel",
    ),
]

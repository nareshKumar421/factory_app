from django.urls import path

from .views import (
    CompleteFGGateEntryAPI,
    FGGateEntryDeleteAPI,
    FGGatePOListAPI,
    FGReceiptDetailAPI,
    ReceiveFGPOAPI,
)

urlpatterns = [
    path(
        "gate-entries/<int:gate_entry_id>/",
        FGGateEntryDeleteAPI.as_view(),
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/",
        ReceiveFGPOAPI.as_view(),
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/<int:po_receipt_id>/",
        FGReceiptDetailAPI.as_view(),
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/view/",
        FGGatePOListAPI.as_view(),
    ),
    path(
        "gate-entries/<int:gate_entry_id>/complete/",
        CompleteFGGateEntryAPI.as_view(),
    ),
]

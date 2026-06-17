from django.urls import path
from .views import (
    ReceivePOAPI,
    POReceiptDetailAPI,
    POReceiptReplaceAPI,
    RawMaterialGateEntryDeleteAPI,
    GatePOListAPI,
    CompleteGateEntryAPI,
)

urlpatterns = [
    path(
        "gate-entries/<int:gate_entry_id>/",
        RawMaterialGateEntryDeleteAPI.as_view()
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/",
        ReceivePOAPI.as_view()
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/<int:po_receipt_id>/",
        POReceiptDetailAPI.as_view()
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/<int:po_receipt_id>/replace/",
        POReceiptReplaceAPI.as_view()
    ),
    path(
        "gate-entries/<int:gate_entry_id>/po-receipts/view/",
        GatePOListAPI.as_view()
    ),
    path(
        "gate-entries/<int:gate_entry_id>/complete/",
        CompleteGateEntryAPI.as_view()
    ),
]

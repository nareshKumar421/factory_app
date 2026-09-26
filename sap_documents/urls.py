from django.urls import path

from .views import (
    AttachmentDownloadAPI,
    AttachmentLinesAPI,
    DocumentDetailAPI,
    DocumentListAPI,
    DocumentTypesAPI,
    PaymentDraftAPI,
)

urlpatterns = [
    path("types/", DocumentTypesAPI.as_view(), name="sap-documents-types"),
    path("documents/<str:doc_type>/", DocumentListAPI.as_view(), name="sap-documents-list"),
    path(
        "documents/<str:doc_type>/<int:doc_entry>/",
        DocumentDetailAPI.as_view(),
        name="sap-documents-detail",
    ),
    path("payment-drafts/<int:doc_entry>/", PaymentDraftAPI.as_view(), name="sap-documents-payment-draft"),
    path("attachments/<int:abs_entry>/", AttachmentLinesAPI.as_view(), name="sap-documents-attachments"),
    path(
        "attachments/<int:abs_entry>/<int:line>/download/",
        AttachmentDownloadAPI.as_view(),
        name="sap-documents-attachment-download",
    ),
]

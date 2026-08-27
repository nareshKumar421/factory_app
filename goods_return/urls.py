from django.urls import path

from .views import (
    GoodsReturnApproveAPI,
    GoodsReturnAttachmentDetailAPI,
    GoodsReturnAttachmentsAPI,
    GoodsReturnDetailAPI,
    GoodsReturnExpectedAPI,
    GoodsReturnInvoiceRefAPI,
    GoodsReturnInvoiceRefDetailAPI,
    GoodsReturnItemsAPI,
    GoodsReturnReturnableItemsAPI,
    GoodsReturnListCreateAPI,
    GoodsReturnMarkInAPI,
    GoodsReturnReceiveAPI,
    GoodsReturnRejectAPI,
    GoodsReturnSubmitAPI,
    GoodsReturnVehicleAPI,
    GoodsReturnWarehousesAPI,
)

urlpatterns = [
    # Static routes (declared before <int:pk> so they are not swallowed as an id)
    path("gate/expected/", GoodsReturnExpectedAPI.as_view(), name="goods-return-gate-expected"),
    path("gate/<int:pk>/mark-in/", GoodsReturnMarkInAPI.as_view(), name="goods-return-gate-mark-in"),
    path("warehouses/", GoodsReturnWarehousesAPI.as_view(), name="goods-return-warehouses"),

    # Returns
    path("", GoodsReturnListCreateAPI.as_view(), name="goods-return-list-create"),
    path("<int:pk>/", GoodsReturnDetailAPI.as_view(), name="goods-return-detail"),
    path("<int:pk>/invoice-refs/", GoodsReturnInvoiceRefAPI.as_view(), name="goods-return-invoice-refs"),
    path(
        "<int:pk>/invoice-refs/<int:ref_id>/",
        GoodsReturnInvoiceRefDetailAPI.as_view(),
        name="goods-return-invoice-ref-detail",
    ),
    path("<int:pk>/items/", GoodsReturnItemsAPI.as_view(), name="goods-return-items"),
    path(
        "<int:pk>/returnable-items/",
        GoodsReturnReturnableItemsAPI.as_view(),
        name="goods-return-returnable-items",
    ),
    path("<int:pk>/vehicle/", GoodsReturnVehicleAPI.as_view(), name="goods-return-vehicle"),
    path("<int:pk>/attachments/", GoodsReturnAttachmentsAPI.as_view(), name="goods-return-attachments"),
    path(
        "<int:pk>/attachments/<int:attachment_id>/",
        GoodsReturnAttachmentDetailAPI.as_view(),
        name="goods-return-attachment-detail",
    ),
    path("<int:pk>/submit/", GoodsReturnSubmitAPI.as_view(), name="goods-return-submit"),
    path("<int:pk>/receive/", GoodsReturnReceiveAPI.as_view(), name="goods-return-receive"),
    path("<int:pk>/approve/", GoodsReturnApproveAPI.as_view(), name="goods-return-approve"),
    path("<int:pk>/reject/", GoodsReturnRejectAPI.as_view(), name="goods-return-reject"),
]

from django.urls import path

from .views import (
    PlanDetailAPI,
    PlanListAPI,
    PlanRequirementAPI,
    PlanRequirementExportAPI,
    PurchaseOrderApproveAPI,
    PurchaseOrderDetailAPI,
    PurchaseOrderListCreateAPI,
    PurchaseOrderPostAPI,
    VendorListAPI,
    WarehouseListAPI,
)

app_name = "planning_purchase"

urlpatterns = [
    # The plan, read from SAP
    path("plans/", PlanListAPI.as_view(), name="pp-plans"),
    path("plans/<int:abs_id>/", PlanDetailAPI.as_view(), name="pp-plan-detail"),
    path(
        "plans/<int:abs_id>/requirement/",
        PlanRequirementAPI.as_view(),
        name="pp-plan-requirement",
    ),
    path(
        "plans/<int:abs_id>/requirement/export/",
        PlanRequirementExportAPI.as_view(),
        name="pp-plan-requirement-export",
    ),

    # Dropdowns
    path("vendors/", VendorListAPI.as_view(), name="pp-vendors"),
    path("warehouses/", WarehouseListAPI.as_view(), name="pp-warehouses"),

    # Purchase orders raised from a plan
    path(
        "purchase-orders/",
        PurchaseOrderListCreateAPI.as_view(),
        name="pp-purchase-orders",
    ),
    path(
        "purchase-orders/<int:order_id>/",
        PurchaseOrderDetailAPI.as_view(),
        name="pp-purchase-order-detail",
    ),
    path(
        "purchase-orders/<int:order_id>/approve/",
        PurchaseOrderApproveAPI.as_view(),
        name="pp-purchase-order-approve",
    ),
    path(
        "purchase-orders/<int:order_id>/post-to-sap/",
        PurchaseOrderPostAPI.as_view(),
        name="pp-purchase-order-post",
    ),
]

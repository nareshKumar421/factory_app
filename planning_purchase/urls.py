from django.urls import path

from .views import (
    BomItemListAPI,
    CommitmentBreakdownAPI,
    PlanDetailAPI,
    PlanListAPI,
    PlanRequirementAPI,
    PlanProducibleAPI,
    PlanRequirementExportAPI,
    ProducibleSimulateAPI,
    PurchaseOrderApproveAPI,
    PurchaseOrderDetailAPI,
    PurchaseOrderListCreateAPI,
    PurchaseOrderPostAPI,
    VendorListAPI,
    WarehouseListAPI,
)
from .views_exim import MonthlyPlanDetailAPI, MonthlyPlanListAPI, OpenPurchaseOrdersAPI

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
        "plans/<int:abs_id>/producible/",
        PlanProducibleAPI.as_view(),
        name="pp-plan-producible",
    ),
    path(
        "plans/<int:abs_id>/requirement/export/",
        PlanRequirementExportAPI.as_view(),
        name="pp-plan-requirement-export",
    ),

    # A run somebody types in, rather than one the plan implies. POST because
    # the request is a list of lines; it still reads and writes nothing.
    path(
        "producible/simulate/",
        ProducibleSimulateAPI.as_view(),
        name="pp-producible-simulate",
    ),
    path("bom-items/", BomItemListAPI.as_view(), name="pp-bom-items"),

    # Why a committed figure is what it is
    path("commitments/", CommitmentBreakdownAPI.as_view(), name="pp-commitments"),

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
    # SAP's open purchase orders and the monthly plan workbook, from EXIM.
    path("open-pos/", OpenPurchaseOrdersAPI.as_view(), name="pp-open-pos"),
    path("monthly-plans/", MonthlyPlanListAPI.as_view(), name="pp-monthly-plans"),
    path("monthly-plans/<str:pk>/", MonthlyPlanDetailAPI.as_view(), name="pp-monthly-plan"),
]

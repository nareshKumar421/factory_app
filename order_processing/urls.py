from django.urls import path

from .views import (
    DashboardAPI,
    LineIssueAPI,
    MaterialRequirementListAPI,
    OrderCheckStockAPI,
    OrderDetailAPI,
    OrderListAPI,
    OrderTimelineAPI,
    PlanMaterialsAPI,
    ProcurementRequirementListAPI,
    ProductionRequirementDetailAPI,
    ProductionRequirementListAPI,
    SyncAPI,
)

app_name = "order_processing"

urlpatterns = [
    path("dashboard/", DashboardAPI.as_view(), name="op-dashboard"),
    path("orders/", OrderListAPI.as_view(), name="op-orders"),
    path("orders/<int:oms_order_id>/", OrderDetailAPI.as_view(), name="op-order-detail"),
    path("orders/<int:oms_order_id>/timeline/", OrderTimelineAPI.as_view(), name="op-order-timeline"),
    path("orders/<int:oms_order_id>/check-stock/", OrderCheckStockAPI.as_view(), name="op-order-check"),
    path("production/", ProductionRequirementListAPI.as_view(), name="op-production"),
    path("production/<int:pk>/", ProductionRequirementDetailAPI.as_view(), name="op-production-detail"),
    path("materials/", MaterialRequirementListAPI.as_view(), name="op-materials"),
    path("materials/plan/", PlanMaterialsAPI.as_view(), name="op-materials-plan"),
    path("procurement/", ProcurementRequirementListAPI.as_view(), name="op-procurement"),
    path("line-issues/", LineIssueAPI.as_view(), name="op-line-issues"),
    path("sync/", SyncAPI.as_view(), name="op-sync"),
]

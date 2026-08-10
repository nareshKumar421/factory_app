from django.urls import path

from .views import (
    CapacityCheckAPI,
    LeadTimeListAPI,
    MachineCapacityListAPI,
    MaterialMachineMapListAPI,
    ProcurementAlarmsAPI,
    ReferenceImportHistoryAPI,
    ReferenceTemplateUploadAPI,
    SupplyChainDashboardAPI,
    SupplyChainPolicyAPI,
)

app_name = "supply_chain"

urlpatterns = [
    path("dashboard/", SupplyChainDashboardAPI.as_view(), name="sc-dashboard"),
    path("procurement/", ProcurementAlarmsAPI.as_view(), name="sc-procurement"),
    path("capacity/", CapacityCheckAPI.as_view(), name="sc-capacity"),
    path("policy/", SupplyChainPolicyAPI.as_view(), name="sc-policy"),
    path("reference/lead-times/", LeadTimeListAPI.as_view(), name="sc-lead-times"),
    path("reference/machines/", MachineCapacityListAPI.as_view(), name="sc-machines"),
    path("reference/sku-machines/", MaterialMachineMapListAPI.as_view(), name="sc-sku-machines"),
    path("reference/upload/", ReferenceTemplateUploadAPI.as_view(), name="sc-reference-upload"),
    path("reference/imports/", ReferenceImportHistoryAPI.as_view(), name="sc-reference-imports"),
]

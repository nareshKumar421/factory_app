from django.urls import path
from .views import (
    # Dropdowns
    ItemDropdownAPI,
    UoMDropdownAPI,
    WarehouseDropdownAPI,
    BOMDropdownAPI,
    # Plan CRUD + actions
    ProductionPlanListCreateAPI,
    ProductionPlanDetailAPI,
    PostPlanToSAPAPI,
    ClosePlanAPI,
    PlanSummaryAPI,
    # Materials
    PlanMaterialListCreateAPI,
    PlanMaterialDeleteAPI,
    # Weekly plans
    WeeklyPlanListCreateAPI,
    WeeklyPlanDetailAPI,
    # Daily entries
    DailyEntryListCreateAPI,
    DailyEntryDetailAPI,
    DailyEntryAllListAPI,
)

urlpatterns = [
    # ------------------------------------------------------------------
    # Dropdowns (SAP HANA data)
    # ------------------------------------------------------------------
    path('dropdown/items/', ItemDropdownAPI.as_view(), name='pp-dropdown-items'),
    path('dropdown/uom/', UoMDropdownAPI.as_view(), name='pp-dropdown-uom'),
    path('dropdown/warehouses/', WarehouseDropdownAPI.as_view(), name='pp-dropdown-warehouses'),
    path('dropdown/bom/', BOMDropdownAPI.as_view(), name='pp-dropdown-bom'),

    # ------------------------------------------------------------------
    # Summary + cross-plan daily entries
    # ------------------------------------------------------------------
    path('summary/', PlanSummaryAPI.as_view(), name='pp-summary'),
    path('daily-entries/', DailyEntryAllListAPI.as_view(), name='pp-daily-entries-all'),

    # ------------------------------------------------------------------
    # Production plan list + create
    # ------------------------------------------------------------------
    path('', ProductionPlanListCreateAPI.as_view(), name='pp-plan-list-create'),

    # ------------------------------------------------------------------
    # Single plan — detail, update, delete, post-to-SAP, close
    # ------------------------------------------------------------------
    path('<int:plan_id>/', ProductionPlanDetailAPI.as_view(), name='pp-plan-detail'),
    path('<int:plan_id>/post-to-sap/', PostPlanToSAPAPI.as_view(), name='pp-plan-post-to-sap'),
    path('<int:plan_id>/close/', ClosePlanAPI.as_view(), name='pp-plan-close'),

    # ------------------------------------------------------------------
    # Material requirements
    # ------------------------------------------------------------------
    path('<int:plan_id>/materials/', PlanMaterialListCreateAPI.as_view(), name='pp-materials'),
    path(
        '<int:plan_id>/materials/<int:material_id>/',
        PlanMaterialDeleteAPI.as_view(),
        name='pp-material-delete',
    ),

    # ------------------------------------------------------------------
    # Weekly plans
    # ------------------------------------------------------------------
    path(
        '<int:plan_id>/weekly-plans/',
        WeeklyPlanListCreateAPI.as_view(),
        name='pp-weekly-plan-list-create',
    ),
    path(
        '<int:plan_id>/weekly-plans/<int:week_id>/',
        WeeklyPlanDetailAPI.as_view(),
        name='pp-weekly-plan-detail',
    ),

    # ------------------------------------------------------------------
    # Daily production entries (scoped to a weekly plan)
    # ------------------------------------------------------------------
    path(
        'weekly-plans/<int:week_id>/daily-entries/',
        DailyEntryListCreateAPI.as_view(),
        name='pp-daily-entry-list-create',
    ),
    path(
        'weekly-plans/<int:week_id>/daily-entries/<int:entry_id>/',
        DailyEntryDetailAPI.as_view(),
        name='pp-daily-entry-detail',
    ),
]

from django.urls import path
from .views import (
    LabourSheetEnsureAPI,
    LabourSheetDetailAPI,
    LabourSheetItemsAPI,
    LabourSheetSubmitAPI,
    LabourSheetPullBackAPI,
    LabourSheetHolidayAPI,
    LabourSheetHistoryAPI,
    LabourGateBoardAPI,
    LabourGateMarkOutAPI,
    LabourGateUndoOutAPI,
    LabourGateFinalizeAPI,
    LabourGateReopenAPI,
)

urlpatterns = [
    path("sheet/ensure/", LabourSheetEnsureAPI.as_view(), name="labour-sheet-ensure"),
    path("sheet/history/", LabourSheetHistoryAPI.as_view(), name="labour-sheet-history"),
    path("sheet/<int:pk>/", LabourSheetDetailAPI.as_view(), name="labour-sheet-detail"),
    path("sheet/<int:pk>/items/", LabourSheetItemsAPI.as_view(), name="labour-sheet-items"),
    path("sheet/<int:pk>/submit/", LabourSheetSubmitAPI.as_view(), name="labour-sheet-submit"),
    path("sheet/<int:pk>/pull-back/", LabourSheetPullBackAPI.as_view(), name="labour-sheet-pull-back"),
    path("sheet/<int:pk>/holiday/", LabourSheetHolidayAPI.as_view(), name="labour-sheet-holiday"),
    # Gate side
    path("gate/board/", LabourGateBoardAPI.as_view(), name="labour-gate-board"),
    path("gate/out/", LabourGateMarkOutAPI.as_view(), name="labour-gate-out"),
    path("gate/out/undo/", LabourGateUndoOutAPI.as_view(), name="labour-gate-out-undo"),
    path("gate/finalize/", LabourGateFinalizeAPI.as_view(), name="labour-gate-finalize"),
    path("gate/sheet/<int:pk>/reopen/", LabourGateReopenAPI.as_view(), name="labour-gate-reopen"),
]

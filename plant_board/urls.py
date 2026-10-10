from django.urls import path

from .views import PlantBoardAPI
from .views_workforce import PlantBoardWorkforceAPI
from .views_space import PlantBoardSpaceAPI
from .views_plan_stock import PlantBoardPlanStockAPI

app_name = "plant_board"

urlpatterns = [
    path("board/", PlantBoardAPI.as_view(), name="plant-board"),
    # The one thing on this board an operator writes.
    path("workforce/", PlantBoardWorkforceAPI.as_view(), name="plant-board-workforce"),
    # How much floor a thousand pieces takes. The only way to turn the stores'
    # piece count into space used.
    path("space/", PlantBoardSpaceAPI.as_view(), name="plant-board-space"),
    # Which warehouses the month-plan SKU drill counts as stock.
    path(
        "plan-stock-warehouses/",
        PlantBoardPlanStockAPI.as_view(),
        name="plant-board-plan-stock-warehouses",
    ),
]

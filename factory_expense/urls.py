from django.urls import path

from .views import (
    CostTypeOptionsAPI,
    FactoryExpenseBoardAPI,
    FactoryExpenseSettingsAPI,
    MonthlyBudgetDetailAPI,
    MonthlyBudgetListCreateAPI,
    ResolvedRatesAPI,
)

urlpatterns = [
    path("board/", FactoryExpenseBoardAPI.as_view(), name="factory-expense-board"),
    path("settings/", FactoryExpenseSettingsAPI.as_view(), name="factory-expense-settings"),
    # Read-back of the Cost Master rows the board prices with. Rates are
    # created and edited in Admin › Cost Master, never here.
    path("rates/", ResolvedRatesAPI.as_view(), name="factory-expense-rates"),
    # The Cost Master types either tile can be pointed at, for the dropdowns.
    path("cost-types/", CostTypeOptionsAPI.as_view(), name="factory-expense-cost-types"),
    path("budgets/", MonthlyBudgetListCreateAPI.as_view(), name="factory-expense-budgets"),
    path(
        "budgets/<int:pk>/",
        MonthlyBudgetDetailAPI.as_view(),
        name="factory-expense-budget-detail",
    ),
]

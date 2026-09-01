from django.urls import path

from .views import (
    DepartmentOptionsAPI,
    DepartmentSalaryDetailAPI,
    DepartmentSalaryListCreateAPI,
    FactoryExpenseBoardAPI,
    FactoryExpenseSettingsAPI,
    LabourRateDetailAPI,
    LabourRateListCreateAPI,
    MonthlyBudgetDetailAPI,
    MonthlyBudgetListCreateAPI,
)

urlpatterns = [
    path("board/", FactoryExpenseBoardAPI.as_view(), name="factory-expense-board"),
    path("settings/", FactoryExpenseSettingsAPI.as_view(), name="factory-expense-settings"),
    path("departments/", DepartmentOptionsAPI.as_view(), name="factory-expense-departments"),
    path(
        "labour-rates/",
        LabourRateListCreateAPI.as_view(),
        name="factory-expense-labour-rates",
    ),
    path(
        "labour-rates/<int:pk>/",
        LabourRateDetailAPI.as_view(),
        name="factory-expense-labour-rate-detail",
    ),
    path(
        "department-salaries/",
        DepartmentSalaryListCreateAPI.as_view(),
        name="factory-expense-department-salaries",
    ),
    path(
        "department-salaries/<int:pk>/",
        DepartmentSalaryDetailAPI.as_view(),
        name="factory-expense-department-salary-detail",
    ),
    path("budgets/", MonthlyBudgetListCreateAPI.as_view(), name="factory-expense-budgets"),
    path(
        "budgets/<int:pk>/",
        MonthlyBudgetDetailAPI.as_view(),
        name="factory-expense-budget-detail",
    ),
]

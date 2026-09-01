from django.urls import path

from .views import (
    CostTypeListCreateAPI, CostTypeDetailAPI,
    CostRateListCreateAPI, CostRateDetailAPI,
)

urlpatterns = [
    path('cost-types/', CostTypeListCreateAPI.as_view(), name='cost-type-list'),
    path('cost-types/<int:cost_type_id>/', CostTypeDetailAPI.as_view(),
         name='cost-type-detail'),
    path('rates/', CostRateListCreateAPI.as_view(), name='cost-rate-list'),
    path('rates/<int:rate_id>/', CostRateDetailAPI.as_view(),
         name='cost-rate-detail'),
]

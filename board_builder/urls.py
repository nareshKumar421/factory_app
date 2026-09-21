"""
board_builder/urls.py

Mounted at ``/api/v1/dashboards/builder/``.

The slug identifies a board WITHIN a company, so every route here is read
under the ``Company-Code`` header: two companies may each have a board called
"dispatch" and neither can reach the other's.
"""

from django.urls import path

from .views import (
    BoardDataAPI,
    BoardDetailAPI,
    BoardDuplicateAPI,
    BoardListCreateAPI,
    BoardPublishAPI,
    CardCatalogueAPI,
    CarouselBoardsAPI,
)

app_name = "board_builder"

urlpatterns = [
    path("catalogue/", CardCatalogueAPI.as_view(), name="catalogue"),
    path("boards/", BoardListCreateAPI.as_view(), name="boards"),
    path("boards/<slug:slug>/", BoardDetailAPI.as_view(), name="board"),
    # The figures. One endpoint for every board there will ever be -- see
    # views.py for why there is not one per card.
    path("boards/<slug:slug>/data/", BoardDataAPI.as_view(), name="board-data"),
    path("boards/<slug:slug>/publish/", BoardPublishAPI.as_view(), name="board-publish"),
    path(
        "boards/<slug:slug>/duplicate/",
        BoardDuplicateAPI.as_view(),
        name="board-duplicate",
    ),
    path("carousel/", CarouselBoardsAPI.as_view(), name="carousel"),
]

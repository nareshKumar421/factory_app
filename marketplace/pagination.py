"""Shared pagination helpers for the marketplace views.

One implementation of the ``{results, count, page, …}`` envelope (matching
``barcode.views._paginated_response``), used by both ``views.py`` (serializer
rendering) and ``views_sheet.py`` (mapper rendering) via a ``render`` callable.
"""
from rest_framework.response import Response


def positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def paginate(request, qs, render, *, default_size=25, max_size=100):
    """Page ``qs`` into the shared envelope. ``render(rows)`` turns the current
    page's queryset slice into the ``results`` list (list of dicts)."""
    page = positive_int(request.query_params.get("page"), 1)
    page_size = min(positive_int(request.query_params.get("page_size"), default_size), max_size)
    total = qs.count()
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return Response({
        "results": render(qs[start:start + page_size]),
        "count": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "next": page < total_pages,
        "previous": page > 1,
    })

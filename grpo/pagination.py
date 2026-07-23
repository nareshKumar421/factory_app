"""Shared list helpers for the GRPO endpoints: page params, month filter, and a
DRF-style pagination envelope.

This repo has no global DRF pagination configured, and the GRPO views are plain
``APIView``s that hand-build their result lists (with Python-side grouping and
post-filtering that cannot be expressed as a single ORM slice). So rather than
wire up ``pagination_class``, these helpers give every GRPO list the same
contract the frontend already speaks (``PaginationControls`` +
``{results, count, page, page_size, total_pages, next, previous}``) while letting
each view paginate wherever is correct for it -- a queryset slice for clean
querysets, or the post-grouping Python list for the pending lists.
"""
import math

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def get_page_params(request):
    """Parse ``page`` / ``page_size`` query params, clamped to sane bounds."""
    try:
        page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.GET.get("page_size", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    page = max(page, 1)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    return page, page_size


def get_month_params(request):
    """Parse the ``year`` / ``month`` month-filter params.

    Returns ``(year, month)`` as ints, or ``(None, None)`` when the filter is
    absent or malformed (callers treat that as "no month filter"). Both must be
    present together; a lone year or month is ignored.
    """
    year = request.GET.get("year")
    month = request.GET.get("month")
    if not year or not month:
        return None, None
    try:
        year_i = int(year)
        month_i = int(month)
    except (TypeError, ValueError):
        return None, None
    if not (1 <= month_i <= 12):
        return None, None
    return year_i, month_i


def paginate_list(items, page, page_size):
    """Slice an in-memory list and return (page_items, envelope_without_results).

    Used by the lists whose final rows are only known after Python-side grouping
    / post-filtering (the pending lists). ``count`` is the full length before
    slicing so the pager shows the true total.
    """
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], _envelope(total, page, page_size, total_pages)


def paginate_queryset(queryset, page, page_size):
    """Slice a queryset at the DB level and return (page_qs, count, meta).

    Used by the clean history querysets that need no Python post-filtering.
    """
    total = queryset.count()
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    start = (page - 1) * page_size
    end = start + page_size
    return queryset[start:end], total, _envelope(total, page, page_size, total_pages)


def build_page(results, meta):
    """Attach a serialized ``results`` list to a meta envelope from paginate_*."""
    return {"results": results, **meta}


def _envelope(total, page, page_size, total_pages):
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "next": page + 1 if page < total_pages else None,
        "previous": page - 1 if page > 1 else None,
    }

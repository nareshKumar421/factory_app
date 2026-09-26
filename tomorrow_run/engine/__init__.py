"""The plan itself: pure functions over a snapshot of tonight's numbers.

Nothing in this package touches Django, the database or SAP. ``inputs`` is a
plain dict read once at 7 pm (see ``tomorrow_run.inputs``) and every pick
re-runs the same arithmetic over the same snapshot, so the day is re-timed
without a single number moving underneath it.
"""

from .planner import build_plan  # noqa: F401

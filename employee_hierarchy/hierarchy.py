"""
The reporting tree: how it is stored, and every rule about moving in it.

The tree is kept as a **materialised path**. Employee 9, reporting to 4,
reporting to 1, stores ``hierarchy_path = "/1/4/9/"`` and
``hierarchy_level = 3``. Both are derived from ``reporting_manager`` and are
maintained only here -- no view, serializer or admin form sets them.

Why a path and not plain recursion:

============================  ==========================================
Question                      With a path
============================  ==========================================
Everyone under this manager   ``filter(hierarchy_path__startswith=...)``
The chain up to the CEO       one ``filter(id__in=...)``
Would this make a cycle?      one ``str.startswith``
Move a manager and their team one ``UPDATE`` over the subtree
============================  ==========================================

Without it, each of those is a query per level -- fine for a demo, not for the
four thousand employees and twelve levels this module is meant to hold. The
cost is that a move has to rewrite the subtree's paths, which is the single
``UPDATE`` in :func:`move_to_manager` and is why that function is the only way
to change a manager.

Every rule in the brief lands in :func:`validate_manager`, so there is exactly
one place to read to know what the hierarchy forbids.
"""

from django.db.models import Count, F, Max, Value
from django.db.models.functions import Replace
from rest_framework.exceptions import ValidationError

from .constants import MAX_HIERARCHY_DEPTH, MANAGER_ELIGIBLE_STATUSES
from .models import Employee


def path_for(employee_id, manager):
    """The path an employee gets under ``manager`` (``None`` for top level)."""
    if manager is None:
        return f"/{employee_id}/"
    return f"{manager.path_prefix}{employee_id}/"


def level_for(path):
    """Reporting hops from the top, read off a path. ``'/1/4/9/'`` → 3."""
    return len([segment for segment in path.split("/") if segment])


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def subtree(employee, *, include_self=False):
    """Everyone below ``employee``, at any depth, as a queryset.

    One indexed prefix match, so "the CTO's whole organisation" costs the same
    whether it is nine people or nine hundred.
    """
    queryset = Employee.objects.filter(hierarchy_path__startswith=employee.path_prefix)
    if not include_self:
        queryset = queryset.exclude(pk=employee.pk)
    return queryset


def descendant_ids(employee):
    """Ids of everyone below ``employee``. Used by the salary access rules."""
    return set(subtree(employee).values_list("id", flat=True))


def reporting_chain(employee):
    """Everyone above ``employee``, top-most first.

    The chain the brief draws -- Team Lead → Manager → Director → CTO → CEO --
    read out of the path in one query and re-ordered in Python, because SQL has
    no cheap way to sort by "position in this list".
    """
    ancestor_ids = employee.ancestor_ids
    if not ancestor_ids:
        return []
    by_id = {
        person.id: person
        for person in Employee.objects.filter(id__in=ancestor_ids).select_related(
            "department", "designation"
        )
    }
    return [by_id[ancestor_id] for ancestor_id in ancestor_ids if ancestor_id in by_id]


def direct_reports(employee):
    """The people who report straight to them."""
    return Employee.objects.filter(reporting_manager=employee).select_related(
        "department", "designation"
    )


def peers(employee):
    """Everyone else reporting to the same manager.

    A top-level employee has no peers -- two unrelated unit heads are not each
    other's colleagues in any sense this module can prove -- so the answer is
    empty rather than "every other root".
    """
    if employee.reporting_manager_id is None:
        return Employee.objects.none()
    return (
        Employee.objects.filter(reporting_manager_id=employee.reporting_manager_id)
        .exclude(pk=employee.pk)
        .select_related("department", "designation")
    )


def with_report_counts(queryset):
    """Annotate ``direct_report_count`` -- the observed size of each team."""
    return queryset.annotate(direct_report_count=Count("direct_reports", distinct=True))


def subtree_height(employee):
    """How many levels deep ``employee``'s own organisation runs, 0 if none.

    Needed before a move: dropping a three-deep team under someone already at
    level eighteen would push the bottom past the depth the path column can
    hold, and it is better to refuse than to truncate somebody's chain.
    """
    deepest = subtree(employee).aggregate(deepest=Max("hierarchy_level"))["deepest"]
    if deepest is None:
        return 0
    return deepest - employee.hierarchy_level


# ---------------------------------------------------------------------------
# Changing the tree
# ---------------------------------------------------------------------------


def validate_manager(employee, manager):
    """Raise unless ``manager`` may be ``employee``'s reporting manager.

    Every hierarchy rule in the brief, in one place:

    * nobody reports to themselves;
    * no circular reporting -- a manager may not be someone from your own team,
      at any depth, because that would close the loop;
    * managers must be assignable: same company, still in service, and not
      suspended (see :data:`~employee_hierarchy.constants.MANAGER_ELIGIBLE_STATUSES`);
    * the resulting chain must fit inside :data:`MAX_HIERARCHY_DEPTH`.

    ``manager=None`` is always allowed: that is "remove manager", which makes
    the employee a top-level unit head, and the brief wants more than one of
    those to be possible.
    """
    if manager is None:
        return

    if employee.pk and manager.pk == employee.pk:
        raise ValidationError("An employee cannot report to themselves.")

    if manager.company_id != employee.company_id:
        raise ValidationError(
            "A reporting manager must belong to the same company as the employee."
        )

    if manager.employment_status not in MANAGER_ELIGIBLE_STATUSES:
        raise ValidationError(
            f"{manager.full_name} is {manager.get_employment_status_display().lower()} "
            "and cannot be given a team. Pick a manager who is active."
        )

    # The cycle test, and the reason the path exists: if the proposed manager
    # sits anywhere inside this employee's own subtree, the chain would loop.
    if employee.pk and manager.hierarchy_path.startswith(employee.path_prefix):
        raise ValidationError(
            f"{manager.full_name} already reports to {employee.full_name} "
            "(directly or further down). That would create a reporting loop."
        )

    depth = manager.hierarchy_level + 1
    if employee.pk:
        depth += subtree_height(employee)
    if depth > MAX_HIERARCHY_DEPTH:
        raise ValidationError(
            f"That move would make the reporting chain {depth} levels deep; "
            f"the limit is {MAX_HIERARCHY_DEPTH}."
        )


def place(employee, manager, *, save=True):
    """Set a NEW employee's manager, path and level. Returns the employee.

    For an employee that already exists use :func:`move_to_manager` -- it also
    carries their team.
    """
    validate_manager(employee, manager)
    employee.reporting_manager = manager
    if employee.pk:
        employee.hierarchy_path = path_for(employee.pk, manager)
        employee.hierarchy_level = level_for(employee.hierarchy_path)
        if save:
            employee.save(update_fields=["reporting_manager", "hierarchy_path", "hierarchy_level"])
    return employee


def move_to_manager(employee, manager):
    """Re-point ``employee`` at ``manager`` and carry their whole team with them.

    The team moving is the default and not an option: the brief is explicit
    that moving a manager preserves their subordinate hierarchy, and it is also
    the only behaviour that leaves the tree consistent -- the alternative would
    orphan everyone below them mid-transaction.

    The subtree is rewritten in one statement. Every descendant's path starts
    with the employee's old prefix, so swapping that prefix for the new one is
    a single ``REPLACE``, and because ids are unique within a path the prefix
    occurs exactly once per row. Levels all shift by the same amount, so they
    move with one ``F`` expression rather than a value per row.

    Returns the number of descendants that moved with them.
    """
    validate_manager(employee, manager)

    old_prefix = employee.path_prefix
    new_prefix = path_for(employee.pk, manager)
    if old_prefix == new_prefix and employee.reporting_manager_id == getattr(manager, "pk", None):
        return 0

    new_level = level_for(new_prefix)
    level_delta = new_level - employee.hierarchy_level

    followers = Employee.objects.filter(hierarchy_path__startswith=old_prefix).exclude(
        pk=employee.pk
    )
    moved = followers.count()
    if moved:
        followers.update(
            hierarchy_path=Replace("hierarchy_path", Value(old_prefix), Value(new_prefix)),
            hierarchy_level=F("hierarchy_level") + level_delta,
        )

    employee.reporting_manager = manager
    employee.hierarchy_path = new_prefix
    employee.hierarchy_level = new_level
    employee.save(update_fields=["reporting_manager", "hierarchy_path", "hierarchy_level"])
    return moved


def rebuild_paths(company=None):
    """Recompute every path and level from ``reporting_manager``. Returns the count.

    A repair tool, not part of normal operation: the services keep the paths
    right. It exists because a materialised path is a cache of the parent links,
    and anything cached deserves a way to be rebuilt -- after a bulk data
    import, or a hand-edit in the Django admin, which is the one door that
    bypasses the services.

    Walks the tree breadth-first, so a parent's path is always final before its
    children read it.
    """
    queryset = Employee.objects.all()
    if company is not None:
        queryset = queryset.filter(company=company)

    rows = list(queryset.values("id", "reporting_manager_id"))
    children = {}
    roots = []
    known = {row["id"] for row in rows}
    for row in rows:
        parent = row["reporting_manager_id"]
        if parent is None or parent not in known:
            roots.append(row["id"])
        else:
            children.setdefault(parent, []).append(row["id"])

    updates = []
    queue = [(employee_id, f"/{employee_id}/") for employee_id in roots]
    while queue:
        employee_id, path = queue.pop()
        updates.append(Employee(id=employee_id, hierarchy_path=path, hierarchy_level=level_for(path)))
        for child_id in children.get(employee_id, []):
            queue.append((child_id, f"{path}{child_id}/"))

    if updates:
        Employee.objects.bulk_update(updates, ["hierarchy_path", "hierarchy_level"], batch_size=500)
    return len(updates)


# ---------------------------------------------------------------------------
# Shaping the tree for the API
# ---------------------------------------------------------------------------


def build_forest(employees, serialize):
    """Nest a flat list of employees into the org tree the chart draws.

    ``serialize`` turns one employee into the node dict; this function only does
    the shape. Anyone whose manager is not in the list becomes a root, so a
    filtered tree ("just the CTO's organisation") still renders as a tree rather
    than as orphans.

    Each node carries ``subtree_size`` -- the number of people underneath -- so
    the chart can say "42 people" on a collapsed branch without a second call.
    """
    nodes = {}
    order = []
    for employee in employees:
        node = serialize(employee)
        node["children"] = []
        nodes[employee.id] = node
        order.append(employee)

    roots = []
    for employee in order:
        node = nodes[employee.id]
        parent = nodes.get(employee.reporting_manager_id)
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)

    def measure(node):
        total = 0
        for child in node["children"]:
            total += 1 + measure(child)
        node["subtree_size"] = total
        return total

    for root in roots:
        measure(root)
    return roots

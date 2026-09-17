"""
control_boards/feeds.py

The catalogue of board-read rights: one right per DATA FEED a control board
reads, and the operational rights each one mirrors.

THE PROBLEM THIS SOLVES
-----------------------
Putting somebody in a "Dashboards — X" group did not make board X work. The
group's rights ARE the operational module's rights — ``VIEW_DISPATCH_PLANS`` on
the frontend is literally ``dispatch_plans.can_view_dispatch_plans`` — so the
only way to fill a board's cards was to grant the module right, and granting the
module right reveals that module in the sidebar (the frontend's
``hasModulePermission`` shows a module when the user holds ANY permission under
its app label). "Grant the data" and "reveal the module" were the same act.

``accounts/management/commands/setup_dashboard_groups.py`` already named the fix
and why nobody had done it:

    There is no way to grant "the carousel only" without minting a right and
    teaching every one of those APIs to accept it.

This module is that right, minted once per feed instead of once per board, so a
group can be assembled out of exactly the feeds a board needs.

WHY THE APP LABEL MATTERS MORE THAN THE CODENAMES
-------------------------------------------------
Every right here lives under the ``control_boards`` app label, and that is
load-bearing rather than tidy. The frontend sidebar prefix-matches: a nav item
carrying ``modulePrefix: 'dispatch_plans'`` appears the moment the user holds any
``dispatch_plans.*`` permission. Minting these rights inside the operational apps
would therefore have re-opened the exact hole they exist to close. No nav item
uses ``control_boards`` as a prefix, and none ever should.

THE RULE, WHICH IS THE WHOLE SECURITY BOUNDARY
-----------------------------------------------
**A feed right is only ever honoured inside a composed board service.** It must
never appear in a ``permission_classes`` list on an operational view. The moment
it does, "one right per feed" becomes the module right wearing a different name,
and the breadth is hidden inside a permission class instead of being visible as
group membership an administrator can audit.

This is ``admin_board/carousel.py``'s rule, restated per-feed. That file also
records what makes honouring a narrow right SAFE, and it is worth repeating
because it is the precondition for everything here (``admin_board/views.py``):

    Safe to widen HERE specifically because this view is the board -- the whole
    thing is composed server-side behind this one read, so the carousel right
    buys exactly this board and nothing adjacent.

A board that fans out from the browser to a dozen operational endpoints cannot
be granted this way, which is why composing a board server-side and granting it
narrowly are one job rather than two.

MIRRORS ARE A TUPLE, NOT A STRING
----------------------------------
``may_read`` accepts the board right OR any operational right the feed mirrors,
which is what keeps every existing user's access exactly as it was — the same
``|`` composition ``admin_board/carousel.py`` uses, applied per feed. Several
feeds genuinely accept rights from a DIFFERENT app than the one that owns the
endpoint (the GRPO pending queue accepts a ``dispatch_plans`` right; packing
material accepts a ``production_execution`` one), so an "app label equals feed
owner" assumption would silently lock people out.

WHAT IS DELIBERATELY NOT MIRRORED
----------------------------------
Three rights guard a READ today despite being write rights —
``goods_return.can_gate_in_goods_return``, ``labour_count.can_verify_labour_count``
and ``employee_hierarchy.can_manage_org_structure``. Only the last is mirrored
here, and only because it is one of four alternatives on an endpoint that also
accepts a real view right. The other two are not: a board viewer should not need
a write right to see a number, which is the VIEW-RIGHTS-ONLY rule
``setup_dashboard_groups`` already follows.

Settings endpoints are not mirrored either. ``WarehouseBoardSettingsAPI`` and
``LogisticsBoardSettingsAPI`` accept **PUT** on ``can_view_stock_dashboard``, so
honouring the ``stock`` feed right there would hand a wall screen a write. Board
services read those settings through the model instead.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Django app label every right here hangs off. See the module docstring --
#: changing this re-opens the sidebar leak.
APP_LABEL = "control_boards"

#: The synthetic content type these permissions hang off. ``boardfeed`` is not a
#: model and never will be; it is a stable ``(app_label, model)`` pair for
#: ``auth_permission.content_type_id`` to point at, the same technique
#: ``admin_board.0001`` and ``gate_core.0059`` use.
CONTENT_TYPE_MODEL = "boardfeed"


@dataclass(frozen=True)
class Feed:
    """One feed a board reads, and who may read it.

    ``mirrors`` is the set of EXISTING rights that already open this data
    somewhere in the product. Holding any of them is accepted in place of the
    board right so that nobody's access narrows the day this ships.
    """

    codename: str
    #: Existing operational rights accepted in place of the board right.
    mirrors: tuple[str, ...] = ()
    #: Shown in the Django admin's permission picker.
    label: str = ""
    #: Why this feed exists and what it discloses. Read by humans only.
    note: str = ""

    @property
    def right(self) -> str:
        """The dotted permission string, as ``user.has_perm`` wants it."""
        return f"{APP_LABEL}.{self.codename}"


def _feed(name: str, *mirrors: str, label: str = "", note: str = "") -> Feed:
    return Feed(
        codename=f"can_read_{name}_feed",
        mirrors=tuple(mirrors),
        label=label or f"Board feed: {name.replace('_', ' ')}",
        note=note,
    )


#: The catalogue. Keys are feed names used by board services and by the
#: frontend's ``BOARD_FEED_PERMISSIONS``; they are part of the contract, so
#: rename one only by changing both sides and the groups together.
FEEDS: dict[str, Feed] = {
    "stock": _feed(
        "stock",
        "stock_dashboard.can_view_stock_dashboard",
        note="SAP warehouse stock, occupancy and stock-in-transit. Does NOT "
        "cover the board-settings endpoints, which accept PUT on the same "
        "right -- board services read those settings through the model.",
    ),
    "non_moving": _feed(
        "non_moving",
        "non_moving_rm.can_view_non_moving_rm",
    ),
    "production_plan": _feed(
        "production_plan",
        "planning_purchase.can_view_production_plan",
    ),
    "production_reports": _feed(
        "production_reports",
        "production_execution.can_view_reports",
        note="The widest production read: reconciliation, movement, daily "
        "output and analytics. Also what opens the packing material board.",
    ),
    "production_cost": _feed(
        "production_cost",
        "production_execution.can_view_run_cost",
        note="Deliberately NOT production_reports. Cost analysis is gated on "
        "its own right in production_execution and must stay separable -- a "
        "shift supervisor reads output without reading what it cost.",
    ),
    "dispatch_plans": _feed(
        "dispatch_plans",
        "dispatch_plans.can_view_dispatch_plans",
    ),
    "dispatch_pipeline": _feed(
        "dispatch_pipeline",
        "dispatch_plans.can_view_dispatch_pipeline",
        "dispatch_plans.can_view_dispatch_plans",
    ),
    "freight": _feed(
        "freight",
        "dispatch_plans.can_view_open_bilties",
        "dispatch_plans.can_post_transporter_ap_invoice",
        note="Freight rates, transporter account and open bilties -- the three "
        "reads share one permission class upstream.",
    ),
    "factory_expense": _feed(
        "factory_expense",
        "factory_expense.can_view_factory_expense",
        "factory_expense.can_configure_factory_expense",
        note="The factory's wage and power bill in total. No per-employee "
        "figure is exposed; see admin_board/permissions.py for the disclosure "
        "the business accepted knowingly.",
    ),
    "wms_space": _feed(
        "wms_space",
        note="MIRRORS NOTHING ON PURPOSE. wms.permissions.WmsCollectionPermission "
        "returns True for every safe method, so warehouses, locations, pallets "
        "and cell purposes are readable today by ANY authenticated user with "
        "company context -- WMS_ACCESS is a frontend-only gate. This right is "
        "the first read gate that data has ever had. It closes a hole; it does "
        "not relax one.",
    ),
    "labour": _feed(
        "labour",
        "labour_gate.view_labourgateentry",
        note="The labour gate day board. labour_count's gate board is NOT "
        "mirrored: it is gated on can_verify_labour_count, a write right.",
    ),
    "workforce": _feed(
        "workforce",
        "employee_hierarchy.can_view_employees",
        "employee_hierarchy.can_view_workforce_reports",
        "employee_hierarchy.can_manage_employees",
        "employee_hierarchy.can_manage_org_structure",
        note="Head count only, off the employee meta endpoint. Salary reads "
        "are a separate family of rights and are not mirrored here.",
    ),
    "grpo": _feed(
        "grpo",
        "grpo.can_view_pending_grpo",
        "grpo.add_grpoposting",
        "dispatch_plans.can_post_bilty_service_grpo",
        note="Cross-app by design: the service-pending queue lives in grpo but "
        "its permission class lives in dispatch_plans.",
    ),
    "docking_scan": _feed(
        "docking_scan",
        "docking_admin.can_view_docking_partial_scan",
    ),
    "pf_movement": _feed(
        "pf_movement",
        "warehouse.can_view_pf_movement",
    ),
    "gate": _feed(
        "gate",
        "gate_core.can_view_gate_dashboard",
    ),
    "dispatch_tracking": _feed(
        "dispatch_tracking",
        "gate_core.can_view_dispatch_tracking",
    ),
    "sales_dispatch_out": _feed(
        "sales_dispatch_out",
        "gate_core.can_view_sales_dispatch_out",
    ),
    "goods_return": _feed(
        "goods_return",
        "goods_return.can_view_goods_return",
        note="The returns dashboard. The gate expected/history reads are NOT "
        "mirrored: they are gated on can_gate_in_goods_return, a write right.",
    ),
    "budget_approvals": _feed(
        "budget_approvals",
        "budget_approvals.can_view_budget_approvals",
    ),
    "blowing": _feed(
        "blowing",
        "blowing.can_view_blowing_reports",
    ),
    "sales_plan_req": _feed(
        "sales_plan_req",
        "sales_planning_requirement.can_view_sales_planning_requirement",
        note="The report, status and analysis reads. Refresh is a write and is "
        "not mirrored.",
    ),
}


def feed(name: str) -> Feed:
    """Look one up, loudly.

    A typo in a feed name inside a board service would otherwise read as
    "withheld" and show an operator an empty tile with no way to tell why.
    """
    try:
        return FEEDS[name]
    except KeyError:
        raise KeyError(
            f"Unknown board feed {name!r}. Known feeds: {', '.join(sorted(FEEDS))}"
        ) from None


def right(name: str) -> str:
    """The dotted permission string for one feed."""
    return feed(name).right


def all_rights() -> tuple[str, ...]:
    """Every board-read right, sorted. The union group is built from this."""
    return tuple(sorted(f.right for f in FEEDS.values()))


def rights_for(*names: str) -> tuple[str, ...]:
    """The rights a board needs, sorted and de-duplicated."""
    return tuple(sorted({right(n) for n in names}))


def may_read(user, name: str) -> bool:
    """Whether this user may read one feed through a composed board.

    True for the board right, and equally true for any operational right the
    feed mirrors -- that second branch is what makes this change invisible to
    everybody who can already read these boards.

    Anonymous short-circuits before ``has_perm``, which matters because
    ``AnonymousUser.has_perm`` happily consults backends and some of ours hit
    the database.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    f = feed(name)
    if user.has_perm(f.right):
        return True
    return any(user.has_perm(mirror) for mirror in f.mirrors)


def readable(user, *names: str) -> set[str]:
    """The subset of ``names`` this user may read. One pass, for a board build."""
    return {n for n in names if may_read(user, n)}

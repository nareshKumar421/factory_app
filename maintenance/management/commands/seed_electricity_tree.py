"""Place the campus's meters in the tree, the way the factory described it.

    python manage.py seed_electricity_tree            # dry run: prints the plan
    python manage.py seed_electricity_tree --apply    # writes it

Only meters that are not placed yet are touched, so a second run changes
nothing and a meter someone has since re-split on Daily Electricity++ is left
exactly as they left it. The Daily Electricity page is left as it is: the tree
is kept in its own tables, and meters are matched by name without being
renamed. The only fields written on an existing meter are KVAH's link to KWH
and its "counts as supply" flag, turned off, as live already has it. A meter
the tree needs and the register lacks (Gov Main, for one) is created, and it
shows on both pages, with no companies, until someone reads it.

Each meter's first version starts on the day the register started reading it
— the tree was always wired this way, so history is split by it too. A meter
the register has never read, and the meters created here, start on ``--start``
(today unless given); a parent created here starts early enough to hold the
sub-meters under it.

What the plan says, and where it is still a guess
-------------------------------------------------
Straight from the factory (2026-09-24/25): KWH and KVAH are two registers of
the main meter; the ground floor, first floor, terrace, boiler, pouch machine,
oil storage, LP-212 (the register's "LP-196") and HP-196 hang off it; HP-512
hung off it too until it was moved onto the Gov Main meter — ``--hp512-moved-on``,
by default 23 Sep 2026, the first day the readings no longer show it inside
KWH; basement, lab, labeler and the ground-floor Sidel hang off the ground
floor; the 1/4 Sidel off the first floor; RO, TR 125 and TR 40 off the terrace.
Who pays for each is as told, and the two chillers follow the run hours of the
two Sidels because nobody knows how they divide (Beverages is entering the
Sidel runs it did not log).

Marked "not decided" or "assumed" in the notes, for the factory to settle on
the Meter Tree page: what the rest of KWH and of Gov Main is (the direct
connections), the rest of the terrace (assumed Beverages, as the register had
it), where ETP and STP hang (assumed off KWH), and LP-212's "maybe 50/50".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Min
from django.utils import timezone
from django.utils.dateparse import parse_date

from company.models import Company
from maintenance.electricity.setup import save_setup
from maintenance.models import (
    DailyElectricityReading,
    ElectricityAllocationBasis as Basis,
    ElectricityMeter,
    SupplySource,
)

OIL, BEV = "JIVO_OIL", "JIVO_BEVERAGES"
HALF = ((OIL, 50), (BEV, 50))


def normalise(name: str) -> str:
    """Case, spaces and punctuation dropped: "HP-196" and "hp 196" are one meter."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


@dataclass
class Node:
    name: str
    parent: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    basis: str = Basis.FIXED
    shares: Tuple[Tuple[str, int], ...] = ()
    #: For RUN_HOURS: ("line" | "blowing", name, company code).
    follows: Tuple[Tuple[str, str, str], ...] = ()
    note: str = ""
    register_of: Optional[str] = None
    #: A later parent, from ``--hp512-moved-on``: a second version moves the
    #: meter under it, and earlier days keep the first parent.
    moved_to: Optional[str] = None
    create: bool = False
    location: str = ""
    supply_source: str = ""
    children: List["Node"] = field(default_factory=list)


PLAN: List[Node] = [
    Node(
        "KWH",
        basis=Basis.UNASSIGNED,
        supply_source=SupplySource.GRID,
        note=(
            "Main meter, KWH register. Its rest is the direct connections off the "
            "main meter; who pays for them is not decided yet."
        ),
    ),
    Node("KVAH", register_of="KWH", note="The main meter's KVAH register."),
    Node(
        "Gov Main",
        aliases=("Main Government", "Government Main", "Govt Main"),
        basis=Basis.UNASSIGNED,
        create=True,
        supply_source=SupplySource.GRID,
        note=(
            "Main government meter. Its rest is the direct connections off it; who "
            "pays for them is not decided yet."
        ),
    ),
    # --- off the main meter ------------------------------------------------
    Node(
        "Production Floor Beverage",
        parent="KWH",
        aliases=("Ground Floor",),
        shares=((BEV, 100),),
        note="Ground floor. The rest after its sub-meters is Beverages' production.",
    ),
    Node(
        "Production Floor OIL",
        parent="KWH",
        aliases=("First Floor",),
        shares=((OIL, 100),),
        note="First floor. The entire floor is Oil's.",
    ),
    Node(
        "Terrace",
        parent="KWH",
        shares=((BEV, 100),),
        note="The rest after RO and the chillers — assumed Beverages, as the register had it.",
    ),
    Node("Boiler", parent="KWH", shares=((BEV, 100),)),
    Node(
        "Pouch  packing meter Fg warehouse",
        parent="KWH",
        aliases=("Pouch machine", "Pouch packing meter Fg warehouse", "Pouch packing FG warehouse"),
        shares=((OIL, 100),),
        note="Pouch and tin machine.",
    ),
    Node(
        "Oil storage meter",
        parent="KWH",
        aliases=("Oil storage", "RM storage"),
        shares=((OIL, 100),),
        note="Oil storage / RM storage.",
    ),
    Node(
        "LP-212",
        parent="KWH",
        aliases=("LP-196", "LP196", "LP212", "LP"),
        shares=HALF,
        note="Serves both plants — \"maybe 50/50\". Its real name is LP-212; the register may still call it LP-196.",
    ),
    Node("HP-196", parent="KWH", aliases=("HP196",), shares=((OIL, 100),)),
    Node("ETP", parent="KWH", shares=((BEV, 100),), note="Assumed off the main meter."),
    Node("STP", parent="KWH", shares=HALF, note="Assumed off the main meter."),
    # --- off KWH, then off the Gov Main meter ------------------------------
    Node(
        "HP-512",
        parent="KWH",
        moved_to="Gov Main",
        aliases=("HP512",),
        shares=((BEV, 100),),
        note="A sub-meter of KWH until it was moved onto the Gov Main meter.",
    ),
    # --- off the ground floor ---------------------------------------------
    Node("Basement", parent="Production Floor Beverage", shares=((OIL, 100),)),
    Node("Lab", parent="Production Floor Beverage", aliases=("LB",), shares=HALF),
    Node("Labeler", parent="Production Floor Beverage", shares=((BEV, 100),), create=True, location="Ground Floor"),
    Node(
        "Sidel (Ground Floor)",
        parent="Production Floor Beverage",
        aliases=("Sidel GF", "Ground Floor Sidel"),
        shares=((BEV, 100),),
        create=True,
        location="Ground Floor",
        note="Beverages' Sidel.",
    ),
    # --- off the first floor ----------------------------------------------
    Node("1/4 Sidel", parent="Production Floor OIL", aliases=("Sidel 1/4",), shares=((OIL, 100),)),
    # --- off the terrace --------------------------------------------------
    Node("Ro meter", parent="Terrace", aliases=("RO",), shares=((BEV, 100),)),
    Node(
        "TR 125",
        parent="Terrace",
        basis=Basis.RUN_HOURS,
        follows=(("blowing", "Synergy 1/4", OIL), ("line", "Sidel", BEV)),
        shares=HALF,
        note=(
            "Chiller serving both Sidels; nobody knows how it divides, so it follows "
            "their run hours. Half each on a day neither logged a run."
        ),
    ),
    Node(
        "TR 40",
        parent="Terrace",
        basis=Basis.RUN_HOURS,
        follows=(("blowing", "Synergy 1/4", OIL), ("line", "Sidel", BEV)),
        shares=HALF,
        note=(
            "Chiller serving both Sidels; nobody knows how it divides, so it follows "
            "their run hours. Half each on a day neither logged a run."
        ),
    ),
]


class Command(BaseCommand):
    help = "Place the campus's electricity meters in the meter tree (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the plan. Without it nothing is saved.")
        parser.add_argument(
            "--start",
            help="First day for meters the register has never read, and for new ones (default today).",
        )
        parser.add_argument(
            "--hp512-moved-on",
            default="2026-09-23",
            help=(
                "The day HP-512 was moved from KWH onto the Gov Main meter (default "
                "2026-09-23 — the readings show it inside KWH up to the 22nd)."
            ),
        )

    def handle(self, *args, **options):
        start = parse_date(options["start"]) if options.get("start") else timezone.localdate()
        if start is None:
            raise CommandError("--start takes a date like 2026-09-24.")
        moved_on = parse_date(options["hp512_moved_on"])
        if moved_on is None:
            raise CommandError("--hp512-moved-on takes a date like 2026-09-23.")

        companies = {company.code: company for company in Company.objects.filter(code__in=(OIL, BEV))}
        missing = {OIL, BEV} - set(companies)
        if missing:
            raise CommandError(f"Company {', '.join(sorted(missing))} does not exist here.")

        meters = {normalise(meter.name): meter for meter in ElectricityMeter.objects.all()}
        first_read = dict(
            DailyElectricityReading.objects.filter(is_active=True)
            .values_list("meter_id")
            .annotate(first=Min("date"))
            .values_list("meter_id", "first")
        )

        by_name = {node.name: node for node in PLAN}
        found = {}
        for node in PLAN:
            for key in (node.name, *node.aliases):
                if normalise(key) in meters:
                    found[node.name] = meters[normalise(key)]
                    break

        # When each node's first version starts: a meter's first reading; a
        # created parent early enough for everything under it.
        def own_start(node):
            meter = found.get(node.name)
            return first_read.get(meter.id, start) if meter else start

        starts = {node.name: own_start(node) for node in PLAN}
        # A parent is in the tree at least as early as anything placed under
        # it, whether from the start or by a later move. Settled until nothing
        # moves, so a grandparent follows its grandchildren too.
        changed = True
        while changed:
            changed = False
            for node in PLAN:
                for parent, since in ((node.parent, starts[node.name]), (node.moved_to, moved_on)):
                    if parent and since < starts[parent]:
                        starts[parent] = since
                        changed = True

        self.stdout.write(self.style.MIGRATE_HEADING(
            "Meter tree plan" + ("" if options["apply"] else " (dry run — nothing will be saved)")
        ))
        placed_already = []
        todo = []
        # What a meter may hang off: one that exists, or one being made now.
        # PLAN lists parents before their sub-meters, so this fills in as the
        # loop goes.
        available = set(found)
        for node in PLAN:
            meter = found.get(node.name)
            status_text = "found" if meter else ("will be created" if node.create else "NOT FOUND")
            if meter is not None and meter.name != node.name:
                # Matched by alias and left as named: the meter is the Daily
                # Electricity page's too, and a rename is for somebody to do
                # on purpose, not a side effect of placing it.
                status_text = f"found as {meter.name!r}"
            if meter is not None and (meter.setups.exists() or (node.register_of and meter.register_of_id)):
                placed_already.append(node.name)
                continue
            if meter is None and not node.create:
                self.stdout.write(self.style.WARNING(f"  {node.name}: not in the register, skipped"))
                continue
            missing = [n for n in (node.parent, node.register_of) if n and n not in available]
            if missing:
                self.stdout.write(self.style.WARNING(
                    f"  {node.name}: {missing[0]} is not in the register, so this is skipped too"
                ))
                continue
            if node.moved_to and node.moved_to not in available:
                self.stdout.write(self.style.WARNING(
                    f"  {node.name}: {node.moved_to} is not in the register, so the move is left out"
                ))
                node = replace(node, moved_to=None)
            available.add(node.name)
            todo.append(node)
            depth = self._depth(node, by_name)
            where = (
                f"second register of {node.register_of}"
                if node.register_of
                else (f"under {node.parent}" if node.parent else "main meter")
            )
            move = f"  → under {node.moved_to} from {moved_on}" if node.moved_to else ""
            self.stdout.write(
                f"  {'  ' * depth}{node.name:<{34 - 2 * depth}} {where:<34} from {starts[node.name]}  "
                f"{self._rule_text(node)}  [{status_text}]{move}"
            )

        for name in placed_already:
            self.stdout.write(self.style.NOTICE(f"  {name}: already placed — left as it is"))
        unplanned = sorted(
            meter.name
            for meter in meters.values()
            if meter not in found.values() and not meter.setups.exists() and meter.register_of_id is None
        )
        for name in unplanned:
            self.stdout.write(self.style.WARNING(f"  {name}: not in the plan — left unplaced"))

        if not options["apply"]:
            self.stdout.write("\nRun again with --apply to save it.")
            return

        with transaction.atomic():
            for node in self._parents_first(todo, by_name):
                self._place(node, found, starts, companies, moved_on)
        self.stdout.write(self.style.SUCCESS(f"Placed {len(todo)} meter(s)."))

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _depth(node, by_name) -> int:
        depth, cursor = 0, node
        while cursor.parent and depth < 10:
            depth += 1
            cursor = by_name[cursor.parent]
        return depth

    @staticmethod
    def _rule_text(node) -> str:
        if node.register_of:
            return "never counted"
        if node.basis == Basis.UNASSIGNED:
            return "who pays: not decided yet"
        shares = " / ".join(f"{'Oil' if code == OIL else 'Bev'} {pct}%" for code, pct in node.shares)
        if node.basis == Basis.RUN_HOURS:
            follows = ", ".join(name for _, name, _ in node.follows)
            return f"by run hours of {follows}; else {shares}"
        return shares

    @staticmethod
    def _parents_first(nodes, by_name):
        done, ordered = set(), []
        pending = list(nodes)
        wanted = {node.name for node in nodes}
        while pending:
            progressed = False
            for node in list(pending):
                blockers = [
                    n
                    for n in (node.parent, node.register_of, node.moved_to)
                    if n and n in wanted and n not in done
                ]
                if not blockers:
                    ordered.append(node)
                    done.add(node.name)
                    pending.remove(node)
                    progressed = True
            if not progressed:
                raise CommandError("The plan has a loop in it: " + ", ".join(n.name for n in pending))
        return ordered

    def _place(self, node, found, starts, companies, moved_on):
        from blowing.models import BlowingMachine
        from production_execution.models import ProductionLine

        meter = found.get(node.name)
        if meter is None:
            meter = ElectricityMeter.objects.create(
                name=node.name,
                location=node.location,
                is_main=node.parent is None,
                supply_source=node.supply_source,
            )
            found[node.name] = meter

        if node.register_of:
            # The Daily Electricity page's own flag says the same thing — a
            # second register is never a supply of its own — so it is kept
            # true there too (live's KVAH already has it off).
            meter.register_of = found[node.register_of]
            meter.counts_as_supply = False
            meter.save(update_fields=["register_of", "counts_as_supply", "updated_at"])
            return

        drivers = []
        basis = node.basis
        note = node.note
        for kind, name, code in node.follows:
            if kind == "line":
                source = ProductionLine.objects.filter(name__iexact=name, company__code=code).first()
                key = "production_line"
            else:
                source = BlowingMachine.objects.filter(name__iexact=name, company__code=code).first()
                key = "blowing_machine"
            if source is None:
                self.stdout.write(self.style.WARNING(
                    f"  {node.name}: {name} ({code}) not found — split fixed instead of by run hours"
                ))
                drivers = []
                basis = Basis.FIXED
                # The note must say what the split IS, not what was meant.
                followed = ", ".join(n for _, n, _ in node.follows)
                note = (
                    f"Meant to follow the run hours of {followed}, but {name} is not "
                    f"a line or blowing machine of {code} here, so it is split at the "
                    f"fixed shares until they are picked on the Meter tree page."
                )
                break
            drivers.append({key: source, "weight": Decimal("1")})

        rule = {
            "basis": basis,
            "shares": [
                {"company": companies[code], "percent": Decimal(pct)} for code, pct in node.shares
            ]
            if basis != Basis.UNASSIGNED
            else [],
            "drivers": drivers,
        }
        # Moved before the register ever read it: it has only ever been where
        # it is now.
        first_parent = node.parent
        if node.moved_to and moved_on <= starts[node.name]:
            first_parent = node.moved_to
        save_setup(
            meter,
            {
                "effective_from": starts[node.name],
                "in_service": True,
                "parent": found[first_parent] if first_parent else None,
                "note": note,
                **rule,
            },
        )
        if node.moved_to and moved_on > starts[node.name]:
            # The move is a second version, so the days before it keep the
            # parent the meter really had then.
            save_setup(
                meter,
                {
                    "effective_from": moved_on,
                    "in_service": True,
                    "parent": found[node.moved_to],
                    "note": f"Moved from {node.parent} onto {node.moved_to}.",
                    **rule,
                },
            )

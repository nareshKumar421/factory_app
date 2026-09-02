"""Resolve a marketplace order into scan-ready component lines.

Expands each order line's SKU via the JI masters (SkuMapping / ComboDefinition):
  * RAW SKU   → one FG line (qty = ordered_quantity)
  * COMBO SKU → one line per component (FG or PM), qty = ordered_quantity × component.quantity

Lines are aggregated by (item_code, component_type). A line that cannot be turned
into at least one item — no mapping, a switched-off combo, a broken option, or no
key to look up at all — is returned in ``unmapped_skus`` and must block dispatch
confirm. "Resolved to nothing" is never a valid answer: it used to pass every gate
and put an order on a delivery note carrying no goods.
"""
import logging
from collections import OrderedDict
from decimal import Decimal

from ..models import ComboComponentType, SkuMapping, SkuType

logger = logging.getLogger(__name__)


def _key(item_code, component_type):
    return (item_code.strip().upper(), component_type)


class MappingIndex:
    """Active mappings for a channel, looked up by FSN first, then marketplace SKU.

    The two keys are held in SEPARATE indexes on purpose. They used to share one
    dict, which broke in two ways:

    * a mapping whose FSN equalled another mapping's marketplace SKU took that key,
      and the second mapping's orders silently shipped the first one's item;
    * keys are upper-cased while the database's uniqueness is case-sensitive, so
      ``sku-a`` and ``SKU-A`` are two legal rows that landed on one key — and which
      of them won was decided by the database's collation, differently under
      PostgreSQL and SQLite.

    Lookup precedence is now explicit rather than emergent: each key is tried
    against its own column first, and only then against the other one (legacy data
    where a sheet's FSN column holds what a mapping stored as its SKU).
    """

    __slots__ = ("by_fsn", "by_sku")

    def __init__(self):
        self.by_fsn = {}
        self.by_sku = {}

    def add(self, mapping):
        for key, bucket, field in (
            ((mapping.fsn or "").strip().upper(), self.by_fsn, "fsn"),
            ((mapping.marketplace_sku or "").strip().upper(), self.by_sku, "marketplace_sku"),
        ):
            if not key:
                continue
            clash = bucket.get(key)
            if clash is not None and clash.pk != mapping.pk:
                # Two rows that differ only in case or spacing. One can never be
                # reached; say which, instead of letting the collation decide.
                logger.warning(
                    "Marketplace mapping %s shadows %s on %s=%r — they differ only in "
                    "case or spacing, so orders for one resolve to the other.",
                    clash.pk, mapping.pk, field, key,
                )
                continue
            bucket[key] = mapping

    def lookup(self, fsn, sku):
        """The mapping for a line's two keys, in a fixed order of preference."""
        fsn = (fsn or "").strip().upper()
        sku = (sku or "").strip().upper()
        return (
            (self.by_fsn.get(fsn) if fsn else None)
            or (self.by_sku.get(sku) if sku else None)
            # Cross matches last: they exist only for data where the columns were
            # filled the other way round, and must never beat an exact match.
            or (self.by_sku.get(fsn) if fsn else None)
            or (self.by_fsn.get(sku) if sku else None)
        )

    def __contains__(self, key):
        key = (key or "").strip().upper()
        return key in self.by_fsn or key in self.by_sku

    @classmethod
    def for_line(cls, line, mapping):
        """A one-entry index resolving THIS line to THIS mapping, with no query."""
        index = cls()
        fsn = (getattr(line, "fsn", "") or "").strip().upper()
        sku = (getattr(line, "marketplace_sku", "") or "").strip().upper()
        if fsn:
            index.by_fsn[fsn] = mapping
        if sku:
            index.by_sku[sku] = mapping
        return index


def load_mappings(company, channel):
    """Active mappings for a channel as a :class:`MappingIndex`.

    Load once and reuse across many orders (e.g. a whole import batch) to avoid a
    per-order query — see ``batch_resolve_service``.
    """
    index = MappingIndex()
    for m in (
        SkuMapping.objects.filter(company=company, channel=channel, is_active=True)
        .select_related("combo")
        .prefetch_related(
            "combo__components", "combo__components__options",
            "options", "options__combo__components", "options__combo__components__options",
        )
    ):
        index.add(m)
    return index


def resolve_order(order, mappings=None):
    """Return ``{"resolved_lines": [...], "unmapped_skus": [...]}`` for an order.

    Pass a pre-built ``mappings`` dict (from :func:`load_mappings`) to skip the
    lookup query when resolving many orders. Each resolved line: item_code,
    item_name, component_type, required_quantity (Decimal), uom, warehouse_code,
    source_skus (list).
    """
    if mappings is None:
        mappings = load_mappings(order.company, order.channel)
    return resolve_lines(
        order_lines_for_resolve(order), order.sap_warehouse_code or "", mappings)


# What a caller resolving MANY orders should prefetch so resolve_order costs it
# nothing: the lines, and everything a chosen option reaches through.
RESOLVE_PREFETCH = ("lines", "lines__chosen_option__combo__components")


def order_lines_for_resolve(order):
    """The order's lines, reusing the caller's prefetch cache when it is warm.

    ``order.lines.select_related(...)`` builds a NEW queryset off the related
    manager, and a new queryset walks straight past a caller's
    ``prefetch_related("lines")``. Resolving in a loop therefore cost one query per
    order however carefully the caller prefetched — 2,337 of them on the Orders
    report, 77 seconds, and the same again on Reconciliation.

    A caller resolving a single order has no cache and takes exactly the path it
    always did, deep select_related included.
    """
    cache = getattr(order, "_prefetched_objects_cache", None) or {}
    if "lines" in cache:
        lines = cache["lines"]
    else:
        lines = order.lines.select_related("chosen_option__combo").prefetch_related(
            "chosen_option__combo__components"
        )
    # 'Delete remaining' lines are gone for every resolver — a delivery note must
    # never issue stock for a parcel deleted off its sheet.
    return [l for l in lines if l.is_active]


def effective_option(line, mapping):
    """The SAP item this line should ship as: the operator's ``chosen_option`` if
    set (and it belongs to this mapping), else the mapping's default option, else
    ``None`` (use the mapping's own single ``fg_item_code``/``combo``)."""
    chosen = getattr(line, "chosen_option", None)
    if chosen is not None and chosen.mapping_id == mapping.id:
        return chosen
    opts = list(mapping.options.all())  # ordered default-first
    return opts[0] if opts else None


def effective_component_item(line, comp):
    """``(item_code, item_name, quantity)`` to ship for one combo component.

    Order of precedence: the operator's pick for this line
    (``line.component_choices``) → the component's default option → the
    component's own ``item_code`` (components with no options are unchanged).

    The quantity is the picked option's own ``quantity`` when set, else the
    component's ``quantity`` — so an alternative that ships in a different
    count is honoured.
    """
    opts = list(comp.options.all())  # ordered default-first
    if opts:
        picked_id = (getattr(line, "component_choices", None) or {}).get(str(comp.id))
        chosen = None
        if picked_id is not None:
            chosen = next((o for o in opts if o.id == picked_id), None)
        if chosen is None:
            chosen = opts[0]  # default
        qty = chosen.quantity if chosen.quantity is not None else comp.quantity
        return chosen.item_code, chosen.item_name or comp.item_name, qty
    return comp.item_code, comp.item_name, comp.quantity


def resolve_lines(lines, warehouse_code, mappings):
    """Resolve an arbitrary set of order lines into aggregated FG/PM component
    lines. Used for a whole order (:func:`resolve_order`) and for the single item
    behind one scanned tracking ID (see ``scan_service``)."""
    agg = OrderedDict()  # key -> resolved line dict
    unmapped = []

    def add(item_code, item_name, component_type, qty, uom, source_sku):
        """Record one resolved item. Returns whether it produced anything — a line
        that produces nothing has to be reported, not dropped."""
        if not item_code:
            return False
        k = _key(item_code, component_type)
        if k not in agg:
            agg[k] = {
                "item_code": item_code,
                "item_name": item_name or "",
                "component_type": component_type,
                "required_quantity": Decimal("0"),
                "uom": uom or "",
                "warehouse_code": warehouse_code,
                "source_skus": [],
            }
        line = agg[k]
        line["required_quantity"] += Decimal(qty)
        if source_sku and source_sku not in line["source_skus"]:
            line["source_skus"].append(source_sku)
        if not line["item_name"] and item_name:
            line["item_name"] = item_name
        if not line["uom"] and uom:
            line["uom"] = uom
        return True

    def report(line):
        """Name a line that could not be resolved, so a gate can stop it.

        A line with neither key cannot be named by SKU at all; it is reported by
        its own id rather than skipped, because skipping is what let an order with
        nothing to ship reach a delivery note.
        """
        key = (line.fsn or "").strip() or (line.marketplace_sku or "").strip()
        key = key or f"(order line {line.pk}: no FSN or SKU)"
        if key not in unmapped:
            unmapped.append(key)

    for line in lines:
        ordered = Decimal(line.ordered_quantity)
        mapping = mappings.lookup(line.fsn, line.marketplace_sku)
        if mapping is None:
            report(line)
            continue

        # One FSN can ship as several SAP items; use the operator's pick (or the
        # mapping's default option), falling back to the mapping's own single item.
        opt = effective_option(line, mapping)
        combo = opt.combo if opt is not None else mapping.combo
        is_combo = (
            (opt.sku_type if opt is not None else mapping.sku_type) == SkuType.COMBO
            and combo is not None
        )
        # A combo switched off must stop the line, exactly as a switched-off mapping
        # does. Only the mapping's own is_active was ever checked, so deactivating a
        # combo did nothing at all and it kept shipping.
        if is_combo and not combo.is_active:
            report(line)
            continue

        produced = False
        if is_combo:
            for comp in combo.components.all():
                # A component slot can be filled by several interchangeable SAP
                # items — honour the operator's pick, else the component default.
                # The chosen item may ship in its own quantity.
                code, name, comp_qty = effective_component_item(line, comp)
                produced |= add(
                    code, name, comp.component_type,
                    ordered * Decimal(comp_qty), comp.uom, line.marketplace_sku,
                )
        else:  # RAW
            fg_code = opt.fg_item_code if opt is not None else mapping.fg_item_code
            # NOT falling back to line.sku_name: that is the marketplace's own
            # product title, and it ends up frozen into the posted snapshot and
            # printed under "SAP Item Name". A label that lies is worse than none.
            fg_name = opt.fg_item_name if opt is not None else mapping.fg_item_name
            produced = add(
                fg_code, fg_name, ComboComponentType.FG, ordered,
                mapping.default_uom, line.marketplace_sku,
            )

        # Mapped, but it yielded no item: an empty combo, or an option pointing at
        # nothing (a COMBO option with no combo attached resolves to a blank code).
        # This used to leave both lists empty and the order shipped as an empty note.
        if not produced:
            report(line)

    return {"resolved_lines": list(agg.values()), "unmapped_skus": unmapped}


def fg_lines(resolved_lines):
    return [l for l in resolved_lines if l["component_type"] == ComboComponentType.FG]


def pm_lines(resolved_lines):
    return [l for l in resolved_lines if l["component_type"] == ComboComponentType.PM]

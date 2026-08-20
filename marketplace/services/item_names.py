"""Names for SAP item codes, from what this database already knows.

A mapping saved while the SAP item master is unreachable keeps no name: the save
copies the name from OITM, and there is nothing to copy during an outage. The
Consolidated stock list then shows a column of item codes with a dash beside each
one, which is what the masters rebuilt on 19 Aug look like today.

The authoritative repair is ``mp_backfill_item_names``, which reads OITM and writes
the real names onto the masters — but that needs SAP, and SAP is exactly what is
missing when this happens.

So the screen falls back to the names the database has already recorded for the
same item code, in order of how much they can be trusted:

  1. another master row naming the same code — saved when SAP *was* readable, and
     already corrected against OITM at that point;
  2. a scan of that item — captured on the floor at dispatch time.

Both originate from SAP; neither is re-derived, and neither is the marketplace's
own product title, which is a different thing entirely and must never stand in for
a SAP item name. A code nothing in the database has ever named is left blank,
because inventing one is worse than showing none.
"""
from .. models import (
    ComboComponent,
    ComboComponentOption,
    MarketplaceScan,
    SkuMapping,
    SkuMappingOption,
)


def _fold(pairs, into):
    """Index ``(code, name)`` pairs by upper-cased code, first name winning."""
    for code, name in pairs:
        code = (code or "").strip().upper()
        name = (name or "").strip()
        if code and name:
            into.setdefault(code, name)
    return into


def local_item_names(codes):
    """``{UPPER item code: name}`` for the codes the database can name.

    Only the codes asked for are looked up, and nothing is queried at all when the
    caller has no gaps to fill — a fully named set of masters costs nothing here.
    """
    wanted = {(c or "").strip().upper() for c in codes}
    wanted.discard("")
    if not wanted:
        return {}

    names = {}
    # Masters first: a name stored against a mapping was checked against the SAP
    # item master when it was written.
    _fold(ComboComponent.objects.filter(item_code__in=wanted)
          .exclude(item_name="").values_list("item_code", "item_name"), names)
    _fold(ComboComponentOption.objects.filter(item_code__in=wanted)
          .exclude(item_name="").values_list("item_code", "item_name"), names)
    _fold(SkuMapping.objects.filter(fg_item_code__in=wanted)
          .exclude(fg_item_name="").values_list("fg_item_code", "fg_item_name"), names)
    _fold(SkuMappingOption.objects.filter(fg_item_code__in=wanted)
          .exclude(fg_item_name="").values_list("fg_item_code", "fg_item_name"), names)

    # Then the floor: what was actually scanned out under that code.
    missing = wanted - set(names)
    if missing:
        _fold(MarketplaceScan.objects.filter(item_code__in=missing)
              .exclude(item_name="").values_list("item_code", "item_name"), names)
    return names


def fill_missing_names(lines, code_key="item_code", name_key="item_name"):
    """Fill the blank names in ``lines`` in place, and return them.

    Costs nothing when every line is already named, which is the normal case once
    ``mp_backfill_item_names`` has run against a reachable SAP.
    """
    gaps = [l for l in lines if not (l.get(name_key) or "").strip()]
    if not gaps:
        return lines
    names = local_item_names(l.get(code_key) for l in gaps)
    for line in gaps:
        code = (line.get(code_key) or "").strip().upper()
        if names.get(code):
            line[name_key] = names[code]
    return lines

"""Correct combo components whose quantity was authored as LITRES instead of PIECES.

``ComboComponent.quantity`` is "quantity per 1 combo unit" — a PIECE count. Several
combos were built by typing the pack's LITRE count into it instead. That is invisible
when the component is a 1 LTR item (6 x 1 L = 6 L, right by accident) and silently
over-issues stock when it is not.

Found by reconciling the posted Flipkart delivery notes against SAP DLN1:

  cb005  Extra-Light-3+3L (a 6 L pack)
      FG0000390 qty 6 -> 2   FG0000390 is a 3 LTR tin, so 6 relieved 18 L, not 6 L.
      Confirmed against all 7 delivery notes that sold it.

  CB0023 Jivo-Extra-Pomace-3L+Extra-Light-1L (3 L pomace + 1 L extra light)
      FG0000028 qty 2 -> 3   Accounts exactly for the 1 L/unit pomace shortfall seen
      on delivery notes 1507264761 and 1507264762.

Optionally rescales the MarketplaceScan rows written under the wrong BOM, so the
scan history matches what should have shipped (``--fix-scans``).

This does NOT touch SAP. Stock already relieved by the posted delivery notes has to
be corrected in SAP itself (adjustment or credit) — see --report for the quantities.

Dry-run by default; pass ``--apply`` to write.

    python manage.py mp_fix_combo_litre_quantities              # show what would change
    python manage.py mp_fix_combo_litre_quantities --apply
    python manage.py mp_fix_combo_litre_quantities --fix-scans --apply
"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from marketplace.models import ComboComponent, ComboDefinition, MarketplaceScan

# MarketplaceScan.quantity is DecimalField(decimal_places=3); rescaling by a ratio
# yields full Decimal precision, so quantize before reporting or saving.
Q3 = Decimal("0.001")


def _q(v):
    return Decimal(v).quantize(Q3, rounding=ROUND_HALF_UP)

# (combo code, item code, expected current qty, corrected qty, why)
FIXES = [
    ("cb005", "FG0000390", Decimal("6"), Decimal("2"),
     "6 L pack; FG0000390 is a 3 LTR tin, so qty 6 relieves 18 L"),
    ("CB0023", "FG0000028", Decimal("2"), Decimal("3"),
     "pack carries 3 L of pomace in 1 LTR bottles, not 2"),
]


class Command(BaseCommand):
    help = "Fix combo component quantities authored as litres instead of pieces."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes (otherwise dry run).")
        parser.add_argument("--fix-scans", action="store_true",
                            help="Also rescale MarketplaceScan rows written under the wrong BOM.")

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        planned, skipped, scan_plan = [], [], []

        for code, item, want_now, want_new, why in FIXES:
            combo = ComboDefinition.objects.filter(code=code).first()
            if combo is None:
                skipped.append(f"{code}: combo not found")
                continue
            comps = list(combo.components.filter(item_code=item))
            if len(comps) != 1:
                skipped.append(f"{code}/{item}: expected 1 component, found {len(comps)}")
                continue
            c = comps[0]
            if c.quantity == want_new:
                skipped.append(f"{code}/{item}: already {want_new} — nothing to do")
                continue
            if c.quantity != want_now:
                # Someone has changed it since this fix was written; do not guess.
                skipped.append(
                    f"{code}/{item}: qty is {c.quantity}, expected {want_now} — SKIPPED, "
                    f"re-check by hand")
                continue
            planned.append((combo, c, want_now, want_new, why))

            if opts["fix_scans"]:
                ratio = want_new / want_now
                scans = MarketplaceScan.objects.filter(item_code=item)
                # Only scans that came from THIS combo's SKUs.
                skus = {s.lower() for s in
                        combo.sku_mappings.values_list("marketplace_sku", flat=True)}
                rows = [s for s in scans if (s.source_sku or "").lower() in skus]
                scan_plan.append((item, ratio, rows))

        w = self.stdout.write
        w("=" * 78)
        w("COMBO COMPONENT QUANTITY FIX" + ("  [APPLY]" if apply_ else "  [DRY RUN]"))
        w("=" * 78)
        if not planned:
            w("nothing to change")
        for combo, c, old, new, why in planned:
            w(f"  {combo.code:<10} {c.item_code}  qty {old} -> {new}")
            w(f"             {c.item_name}")
            w(f"             reason: {why}")
        for s in skipped:
            w(f"  SKIP  {s}")

        if opts["fix_scans"]:
            w("")
            w("SCAN ROWS")
            for item, ratio, rows in scan_plan:
                total = sum((r.quantity for r in rows), Decimal("0"))
                w(f"  {item}: {len(rows)} row(s), {total} pc recorded "
                  f"-> {_q(total * ratio)} pc")
                for r in rows[:5]:
                    dn = r.dispatch.sap_delivery_note_num or "(not posted)"
                    w(f"      scan {r.id} DN {dn:<12} {r.quantity} "
                      f"-> {_q(r.quantity * ratio)}")
                if len(rows) > 5:
                    w(f"      ... and {len(rows) - 5} more")

        if not apply_:
            w("")
            w("dry run — nothing written. Re-run with --apply to commit.")
            return

        with transaction.atomic():
            for _combo, c, _old, new, _why in planned:
                c.quantity = new
                c.save(update_fields=["quantity"])
            n_scans = 0
            for _item, ratio, rows in scan_plan:
                for r in rows:
                    r.quantity = _q(r.quantity * ratio)
                    r.save(update_fields=["quantity"])
                    n_scans += 1
        w("")
        w(f"WROTE {len(planned)} component(s)"
          + (f" and {n_scans} scan row(s)" if opts["fix_scans"] else ""))
        w("SAP is untouched — stock already relieved by posted delivery notes still "
          "needs an adjustment there.")

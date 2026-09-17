"""Re-import the factory's stacking sheet into the board's data file.

    manage.py import_stacking "excel/Oil Stacking 11.09.2026.xlsx" --measured-on 2026-09-11

Writes ``plant_board/data/stacking.json``, which is checked in. Run it when the
factory re-measures the range, and commit the diff: the board's occupancy
figure rests on these numbers, so they should move under review rather than
quietly.

Only the item code and the pieces-per-pallet column are taken. The sheet's
quantity column is a snapshot of one morning and the board reads live SAP stock
instead.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Import pieces-per-pallet from the factory's stacking workbook."

    def add_arguments(self, parser):
        parser.add_argument("workbook", help="Path to the .xlsx")
        parser.add_argument("--sheet", default=None, help="Sheet name (default: the first)")
        parser.add_argument("--measured-on", default="", help="YYYY-MM-DD the range was measured")

    def handle(self, *args, **options):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - environment problem
            raise CommandError("openpyxl is needed to read the workbook.") from exc

        path = Path(options["workbook"])
        if not path.exists():
            raise CommandError(f"No such workbook: {path}")

        book = openpyxl.load_workbook(path, data_only=True)
        sheet = book[options["sheet"]] if options["sheet"] else book[book.sheetnames[0]]

        items, groups, skipped = {}, {}, 0
        for row in list(sheet.iter_rows(values_only=True))[1:]:
            if not row or not row[1]:
                continue
            code = str(row[1]).strip()
            try:
                per_pallet = float(row[4] or 0)
            except (TypeError, ValueError):
                per_pallet = 0
            if per_pallet <= 0:
                # Counted, not guessed at. An item with no factor is left out
                # and the board discloses how much stock it could not measure.
                skipped += 1
                continue
            items[code] = per_pallet
            groups[code] = str(row[0]).strip() if row[0] else ""

        target = Path(__file__).resolve().parents[2] / "data" / "stacking.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "source": path.name,
                    "measured_on": options["measured_on"],
                    "unit": "pieces per pallet",
                    "items": dict(sorted(items.items())),
                    "groups": dict(sorted(groups.items())),
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(items)} items written to {target}"
                + (f"; {skipped} rows had no stacking figure" if skipped else "")
            )
        )

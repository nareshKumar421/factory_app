"""Post ONE real SAP Delivery Note through the marketplace production code path.

Uses ``MarketplaceSapGateway`` (simulate forced OFF) — the exact writer the
dispatch-confirm flow uses — so a successful run proves live posting works.

REAL post: creates a real Delivery Note and decrements real stock. Requires
``--confirm`` to actually fire; without it, it only prints the payload (dry run).

    # dry run (prints payload, posts nothing)
    python manage.py mp_post_delivery_note --card-code CUSTA000356 \
        --warehouse DL-EC --item FG0000329 --qty 1

    # real post
    python manage.py mp_post_delivery_note --card-code CUSTA000356 \
        --warehouse DL-EC --item FG0000329 --qty 1 --tax-code CG+SG@18 \
        --ref sample-test --confirm
"""
import json
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from marketplace.services.sap_gateway import MarketplaceSapGateway


class Command(BaseCommand):
    help = "Post one real SAP Delivery Note via the marketplace gateway (needs --confirm)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_MART")
        parser.add_argument("--card-code", required=True, help="SAP customer CardCode")
        parser.add_argument("--warehouse", required=True, help="SAP warehouse code (must hold stock)")
        parser.add_argument("--item", required=True, help="SAP ItemCode")
        parser.add_argument("--qty", default="1")
        parser.add_argument("--tax-code", default="", help="VatGroup, e.g. CG+SG@18")
        parser.add_argument("--series", default="")
        parser.add_argument("--ref", default="mp-test", help="Traceability ref → NumAtCard/Comments")
        parser.add_argument("--confirm", action="store_true", help="Actually POST (else dry run).")

    def handle(self, *args, **o):
        gateway = MarketplaceSapGateway(o["company"])
        gateway.simulate = False  # force the REAL Service Layer path

        fg_lines = [{
            "item_code": o["item"],
            "required_quantity": Decimal(o["qty"]),
            "warehouse_code": o["warehouse"],
        }]
        num_at_card = f"MP-{o['ref']}"
        comments = f"Marketplace delivery-note test · {o['ref']}"

        self.stdout.write(self.style.WARNING("Delivery Note to post:"))
        self.stdout.write(
            "  " + json.dumps({
                "company": o["company"], "CardCode": o["card_code"], "Warehouse": o["warehouse"],
                "Item": o["item"], "Quantity": o["qty"], "VatGroup": o["tax_code"] or "(BP/item default)",
                "Series": o["series"] or "(SAP default)", "NumAtCard": num_at_card,
            }, indent=2).replace("\n", "\n  ")
        )

        if not o["confirm"]:
            self.stdout.write(self.style.NOTICE("\nDRY RUN — nothing posted. Add --confirm to post for real."))
            return

        self.stdout.write(self.style.WARNING("\nPosting to REAL SAP…"))
        try:
            dn = gateway.create_delivery_note(
                ref=1, card_code=o["card_code"], warehouse_code=o["warehouse"],
                fg_lines=fg_lines, doc_date=date.today(),
                num_at_card=num_at_card, comments=comments,
                series=o["series"], tax_code=o["tax_code"],
            )
        except Exception as exc:  # noqa: BLE001 — surface the SAP error verbatim
            raise CommandError(f"SAP rejected the Delivery Note: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"\nPOSTED ✓  DocEntry={dn['DocEntry']}  DocNum={dn['DocNum']}"
        ))
        # Append an audit record.
        record = {
            "posted_at": timezone.now().isoformat(),
            "company": o["company"], "card_code": o["card_code"], "warehouse": o["warehouse"],
            "item": o["item"], "qty": o["qty"], "tax_code": o["tax_code"], "series": o["series"],
            "num_at_card": num_at_card,
            "doc_entry": dn["DocEntry"], "doc_num": dn["DocNum"],
        }
        self.stdout.write("\nAudit record:\n  " + json.dumps(record))

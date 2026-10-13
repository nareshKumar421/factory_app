"""
Turn old "taken back" + "given" pairs into handovers.

Usage::

    python manage.py link_advance_handovers --company JIVO_OIL
        # lists the pairs that look like one handover; changes nothing

    python manage.py link_advance_handovers --company JIVO_OIL \\
        --pair 327:326 --pair 328:325
        # checks those pairs (taken-back id : given id); changes nothing

    python manage.py link_advance_handovers --company JIVO_OIL \\
        --pair 327:326 --pair 328:325 --apply
        # links them

Before the Advances page had "Hand over", cash Kamal passed to Manoj was typed
as cash taken back off Kamal and cash given to Manoj. The balances are already
right; what is missing is that neither row says who the other person was, and
either can be taken out without the other.

Only the pairs named with ``--pair`` are linked. The suggestions are matched on
day and amount, and a return to the box followed by a handout from it looks the
same, so somebody reads the details first.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from rest_framework.exceptions import ValidationError

from cash_book import services
from cash_book.models import AdvanceEntry
from company.models import Company


def _row(entry):
    name = entry.person.full_name or entry.person.email
    return (
        f"#{entry.id:<5} {entry.get_direction_display():<16} "
        f"{entry.amount:>12}  {name[:24]:<24} | {(entry.detail or '')[:70]}"
    )


class Command(BaseCommand):
    help = "Link old taken-back / given pairs into handovers between people."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company code.")
        parser.add_argument(
            "--pair",
            action="append",
            default=[],
            metavar="TAKEN_BACK_ID:GIVEN_ID",
            help="A pair to link. Repeat for more.",
        )
        parser.add_argument(
            "--apply", action="store_true", help="Write the links. Off by default."
        )

    def handle(self, *args, **options):
        try:
            company = Company.objects.get(code=options["company"])
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {options['company']}.")

        if not options["pair"]:
            self._suggest(company)
            return

        pairs = []
        for raw in options["pair"]:
            try:
                out_id, into_id = (int(part) for part in raw.split(":"))
            except ValueError:
                raise CommandError(f"{raw!r} is not TAKEN_BACK_ID:GIVEN_ID.")
            found = AdvanceEntry.objects.filter(
                company=company, id__in=[out_id, into_id]
            ).select_related("person").in_bulk()
            if out_id not in found or into_id not in found:
                raise CommandError(f"{raw}: no such advance rows in {company.code}.")
            pairs.append((found[out_id], found[into_id]))

        try:
            with transaction.atomic():
                for out_of, into in pairs:
                    services.link_handover(user=None, out_of=out_of, into=into)
                    self.stdout.write(f"{out_of.entry_date}")
                    self.stdout.write(f"   {_row(out_of)}")
                    self.stdout.write(f"   {_row(into)}")
                if not options["apply"]:
                    transaction.set_rollback(True)
        except ValidationError as exc:
            raise CommandError(f"Nothing linked. {exc.detail['handover']}")

        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Linked {len(pairs)} handover(s)."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(pairs)} pair(s) check out. Nothing written -- "
                    f"add --apply to link them."
                )
            )

    def _suggest(self, company):
        pairs = services.handover_candidates(company)
        if not pairs:
            self.stdout.write("No pairs look like a handover.")
            return
        for out_of, into in pairs:
            self.stdout.write(f"{out_of.entry_date}   --pair {out_of.id}:{into.id}")
            self.stdout.write(f"   {_row(out_of)}")
            self.stdout.write(f"   {_row(into)}")
        self.stdout.write(
            self.style.WARNING(
                f"{len(pairs)} suggestion(s). Read the details, then pass the "
                f"real ones with --pair. Nothing written."
            )
        )

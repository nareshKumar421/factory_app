"""Safely soft-delete (or hard-delete / restore) a gate entry by entry number.

This is the auditable replacement for hand-written ``manage.py shell`` deletes.

Two removal modes:

* **Soft delete (default)** -- flips ``is_active`` to False and stamps an audit
  note into ``remarks``. The row (and all its QC / PO / security records) stays
  in the database, so the delete is fully **reversible** with ``--restore``. The
  operational screens that already or now filter ``is_active`` hide it:
  the gate list, the QC arrival-slip worklist, and the GRPO dashboard/ready list
  (GRPO already filtered ``is_active`` before this command existed). It cannot be
  GRPO-posted or QC-processed while soft-deleted.

* **Hard delete (``--hard``)** -- cascades the row away for good (PO receipts,
  arrival slip, QC inspection + parameter results, security check). Not
  reversible. Use only when the record must truly disappear.

``--restore`` undoes a soft delete (``is_active`` back to True).

The one truly irreversible line is a **posted GRPO** (a live SAP document): both
delete modes refuse when any PO receipt has a POSTED GRPO. Locked entries are
refused unless ``--force`` is also given.

Everything is **dry-run by default** -- nothing is written without ``--confirm``.

Examples::

    python manage.py delete_gate_entry GE-2026-9163                    # preview soft delete
    python manage.py delete_gate_entry GE-2026-9163 --confirm          # soft delete (reversible)
    python manage.py delete_gate_entry GE-2026-9163 --restore --confirm # undo the soft delete
    python manage.py delete_gate_entry GE-2026-9163 --hard --confirm    # purge for good
    python manage.py delete_gate_entry 9163 --company JIVO_OIL --confirm # suffix + company scope
"""
import getpass
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import router, transaction
from django.db.models.deletion import Collector
from django.utils import timezone

from driver_management.models import VehicleEntry

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Soft-delete (default), hard-delete, or restore a gate entry by entry number."

    def add_arguments(self, parser):
        parser.add_argument(
            "entry_no",
            help="Full entry number (GE-2026-9163) or a unique suffix (9163).",
        )
        parser.add_argument(
            "--company",
            help="Restrict the lookup to a company code (e.g. JIVO_OIL). "
            "Use when a suffix matches entries in more than one company.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Actually perform the action. Without this the command only "
            "previews it (dry run).",
        )
        parser.add_argument(
            "--hard",
            action="store_true",
            help="Hard-delete (cascade) instead of the default reversible soft delete.",
        )
        parser.add_argument(
            "--restore",
            action="store_true",
            help="Undo a soft delete (set is_active back to True).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Act even if the entry is locked. Never overrides the "
            "posted-GRPO guard.",
        )
        parser.add_argument(
            "--by",
            help="Name to record in the audit note (defaults to the OS user).",
        )

    # -- lookup ---------------------------------------------------------------

    def _resolve_entry(self, entry_no, company):
        qs = VehicleEntry.objects.all()
        if company:
            qs = qs.filter(company__code=company)

        matches = list(qs.filter(entry_no=entry_no))
        if not matches:
            # fall back to a suffix match so "9163" finds "GE-2026-9163"
            matches = list(qs.filter(entry_no__endswith=entry_no))

        if not matches:
            raise CommandError(
                f"No gate entry found for '{entry_no}'"
                + (f" in company {company}." if company else ".")
            )
        if len(matches) > 1:
            listing = "\n".join(
                f"  - {e.entry_no} (pk={e.pk}, {e.entry_type}, "
                f"company={getattr(e.company, 'code', e.company_id)})"
                for e in matches
            )
            raise CommandError(
                f"'{entry_no}' matches {len(matches)} entries -- pass the full "
                f"entry number (and/or --company) to disambiguate:\n{listing}"
            )
        return matches[0]

    # -- guards ---------------------------------------------------------------

    def _posted_grpo_blockers(self, entry):
        """Return a list of human-readable POSTED-GRPO blockers, if any."""
        blockers = []
        po_receipts = getattr(entry, "po_receipts", None)
        if po_receipts is None:  # non-RM entry types have no PO receipts
            return blockers
        for po in po_receipts.all():
            posted = list(po.grpo_postings.filter(status="POSTED")) + list(
                po.merged_grpo_postings.filter(status="POSTED")
            )
            for g in posted:
                blockers.append(
                    f"PO {getattr(po, 'po_number', po.pk)} has POSTED GRPO "
                    f"(posting pk={g.pk})"
                )
        return blockers

    def _print_entry(self, entry):
        self.stdout.write(self.style.MIGRATE_HEADING("Gate entry"))
        self.stdout.write(
            f"  entry_no   : {entry.entry_no}\n"
            f"  pk         : {entry.pk}\n"
            f"  type       : {entry.entry_type}\n"
            f"  status     : {entry.status}\n"
            f"  is_active  : {entry.is_active}\n"
            f"  locked     : {entry.is_locked}\n"
            f"  company    : {getattr(entry.company, 'code', entry.company_id)}\n"
            f"  entry_time : {entry.entry_time}"
        )

    def _audit_note(self, action, who):
        return f"[{action} {timezone.now().isoformat(timespec='seconds')} by {who}]"

    # -- main -----------------------------------------------------------------

    def handle(self, *args, **opts):
        if opts["hard"] and opts["restore"]:
            raise CommandError("--hard and --restore are mutually exclusive.")

        entry = self._resolve_entry(opts["entry_no"], opts.get("company"))
        self._print_entry(entry)
        who = opts.get("by") or getpass.getuser()

        if opts["restore"]:
            return self._do_restore(entry, opts, who)
        if opts["hard"]:
            return self._do_hard_delete(entry, opts)
        return self._do_soft_delete(entry, opts, who)

    # -- restore --------------------------------------------------------------

    def _do_restore(self, entry, opts, who):
        if entry.is_active:
            raise CommandError("Entry is not soft-deleted; nothing to restore.")

        self.stdout.write(
            self.style.MIGRATE_HEADING("\nWill RESTORE (set is_active=True)")
        )
        if not opts["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN -- nothing changed. Re-run with --confirm to restore."
                )
            )
            return

        note = self._audit_note("restored", who)
        new_remarks = f"{entry.remarks}\n{note}".strip() if entry.remarks else note
        with transaction.atomic():
            VehicleEntry.objects.filter(pk=entry.pk).update(
                is_active=True, remarks=new_remarks
            )
        logger.warning(
            "Gate entry %s (pk=%s) restored via delete_gate_entry by %s",
            entry.entry_no,
            entry.pk,
            who,
        )
        self.stdout.write(
            self.style.SUCCESS(f"\nRestored {entry.entry_no} (pk={entry.pk}).")
        )

    # -- soft delete ----------------------------------------------------------

    def _do_soft_delete(self, entry, opts, who):
        if not entry.is_active:
            raise CommandError(
                "Entry is already soft-deleted. Use --restore to undo, or "
                "--hard to purge it permanently."
            )
        self._reject_posted_grpo(entry)
        self._reject_locked(entry, opts)

        self.stdout.write(
            self.style.MIGRATE_HEADING("\nWill SOFT-DELETE (reversible)")
        )
        self.stdout.write(
            "  Sets is_active=False. The row and its QC/PO/security records stay\n"
            "  in the DB and are hidden from the gate list, QC worklist and GRPO\n"
            "  screens. Undo any time with --restore."
        )
        if not opts["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN -- nothing changed. Re-run with --confirm to soft-delete."
                )
            )
            return

        note = self._audit_note("soft-deleted", who)
        new_remarks = f"{entry.remarks}\n{note}".strip() if entry.remarks else note
        with transaction.atomic():
            VehicleEntry.objects.filter(pk=entry.pk).update(
                is_active=False, remarks=new_remarks
            )
        logger.warning(
            "Gate entry %s (pk=%s, status=%s) soft-deleted via delete_gate_entry by %s",
            entry.entry_no,
            entry.pk,
            entry.status,
            who,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"\nSoft-deleted {entry.entry_no} (pk={entry.pk}). Reversible with "
                f"--restore."
            )
        )

    # -- hard delete ----------------------------------------------------------

    def _do_hard_delete(self, entry, opts):
        self._reject_posted_grpo(entry)
        self._reject_locked(entry, opts)

        collector = Collector(using=router.db_for_write(VehicleEntry))
        collector.collect([entry])
        counts = {}
        for model, instances in collector.data.items():
            counts[model._meta.label] = counts.get(model._meta.label, 0) + len(
                instances
            )
        total = sum(counts.values())

        self.stdout.write(
            self.style.MIGRATE_HEADING("\nWill HARD-DELETE (cascade, NOT reversible)")
        )
        for label in sorted(counts):
            self.stdout.write(f"  {label}: {counts[label]}")
        self.stdout.write(f"  TOTAL rows: {total}")

        if not opts["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN -- nothing deleted. Re-run with --confirm to hard-delete."
                )
            )
            return

        entry_no, entry_pk, entry_type, entry_status = (
            entry.entry_no,
            entry.pk,
            entry.entry_type,
            entry.status,
        )
        with transaction.atomic():
            deleted_total, per_model = entry.delete()

        logger.warning(
            "Gate entry %s (pk=%s, type=%s, status=%s) HARD-deleted via "
            "delete_gate_entry; %s rows removed: %s",
            entry_no,
            entry_pk,
            entry_type,
            entry_status,
            deleted_total,
            per_model,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"\nHard-deleted {entry_no} (pk={entry_pk}) -- {deleted_total} rows removed."
            )
        )

    # -- shared guards --------------------------------------------------------

    def _reject_posted_grpo(self, entry):
        blockers = self._posted_grpo_blockers(entry)
        if blockers:
            raise CommandError(
                "Refusing to delete -- a posted GRPO (SAP document) exists:\n  "
                + "\n  ".join(blockers)
            )

    def _reject_locked(self, entry, opts):
        if entry.is_locked and not opts["force"]:
            raise CommandError(
                "Entry is locked. Re-run with --force to act on a locked entry."
            )

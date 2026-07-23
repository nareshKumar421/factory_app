"""
Unit tests for the shared document-numbering utility.

Covers: code generation, sequential increment, uniqueness, format validation
and parsing -- the single source of truth used by GATE, QC and GRPO.
"""

from datetime import date

from django.test import TestCase

from . import numbering, services
from .config import MODULE_DOCUMENT_TYPES
from .models import DocumentCode
from .numbering import InvalidDocumentCode


class ParsingTests(TestCase):
    def test_parse_full_code(self):
        parsed = numbering.parse_code("DOC-SOP-04-02-00-01")
        self.assertEqual(parsed.section, "DOC")
        self.assertEqual(parsed.doctype, "SOP")
        self.assertEqual(parsed.cc, "04")
        self.assertEqual(parsed.ss, "02")
        self.assertEqual(parsed.gg, "00")
        self.assertEqual(parsed.nn, "01")
        self.assertEqual(parsed.serial, 1)
        self.assertEqual(parsed.clause, "04-02-00")
        self.assertEqual(parsed.group_key, ("DOC", "SOP", "04", "02", "00"))

    def test_parse_code_without_serial(self):
        parsed = numbering.parse_code("QA-FRM-08-06-00")
        self.assertIsNone(parsed.nn)
        self.assertIsNone(parsed.serial)
        self.assertEqual(str(parsed), "QA-FRM-08-06-00")

    def test_parse_round_trips_through_str(self):
        parsed = numbering.parse_code("STR-FRM-08-05-00-07")
        self.assertEqual(str(parsed), "STR-FRM-08-05-00-07")

    def test_parse_carries_human_names(self):
        parsed = numbering.parse_code("QA-FRM-08-06-00-01")
        self.assertEqual(parsed.section_name, "Quality")
        self.assertEqual(parsed.doctype_name, "Form")


class ValidationTests(TestCase):
    def test_valid_codes(self):
        for code in [
            "DOC-SOP-04-02-00",
            "DOC-SOP-04-02-00-01",
            "QA-FRM-08-06-00-99",
            "TACCP-MAN-01-00-00",
        ]:
            self.assertTrue(numbering.is_valid_code(code), code)

    def test_invalid_codes(self):
        for code in [
            "",
            "DOC-SOP-4-2-0",          # blocks not two digits
            "DOC-SOP-04-02",          # too few blocks
            "ZZZ-SOP-04-02-00",       # unknown section
            "DOC-XXX-04-02-00",       # unknown doctype
            "DOC-SOP-04-02-00-1",     # NN not two digits
            "doc-sop-04-02-00",       # lowercase
            "DOC_SOP_04_02_00",       # wrong separators
        ]:
            self.assertFalse(numbering.is_valid_code(code), code)

    def test_parse_raises_on_bad_format(self):
        with self.assertRaises(InvalidDocumentCode):
            numbering.parse_code("nonsense")

    def test_validate_parts_rejects_unknown_section(self):
        with self.assertRaises(InvalidDocumentCode):
            numbering.validate_parts("ZZZ", "FRM", "08-05-00")

    def test_validate_parts_rejects_bad_clause(self):
        with self.assertRaises(InvalidDocumentCode):
            numbering.validate_parts("QA", "FRM", "8-5-0")

    def test_format_code_zero_pads_serial(self):
        self.assertEqual(
            numbering.format_code("STR", "FRM", "08-05-00", 3),
            "STR-FRM-08-05-00-03",
        )
        self.assertEqual(
            numbering.format_code("STR", "FRM", "08-05-00"),
            "STR-FRM-08-05-00",
        )


class GenerationTests(TestCase):
    def test_first_code_is_serial_01(self):
        doc = services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        self.assertEqual(doc.code, "QA-FRM-08-06-00-01")
        self.assertEqual(doc.nn, 1)
        self.assertEqual(doc.revision_number, 0)
        self.assertEqual(doc.revision_label, "00")
        self.assertEqual(doc.issue_date, date.today())
        self.assertEqual(doc.total_pages, 1)

    def test_sequential_increment_within_group(self):
        codes = [
            services.allocate_code(
                section="STR", doctype="FRM", clause="08-05-00"
            ).code
            for _ in range(3)
        ]
        self.assertEqual(
            codes,
            ["STR-FRM-08-05-00-01", "STR-FRM-08-05-00-02", "STR-FRM-08-05-00-03"],
        )

    def test_increment_is_per_group_not_global(self):
        a = services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        b = services.allocate_code(section="STR", doctype="FRM", clause="08-05-00")
        # Different groups both start at 01.
        self.assertEqual(a.nn, 1)
        self.assertEqual(b.nn, 1)

    def test_peek_does_not_consume(self):
        self.assertEqual(services.peek_next_code("QA", "FRM", "08-06-00"),
                         "QA-FRM-08-06-00-01")
        services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        # Peeking again reflects the one already issued, without issuing another.
        self.assertEqual(services.peek_next_code("QA", "FRM", "08-06-00"),
                         "QA-FRM-08-06-00-02")
        self.assertEqual(DocumentCode.objects.count(), 1)

    def test_next_serial_is_max_plus_one_even_with_gaps(self):
        services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        second = services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        second.delete()  # leave a gap: only NN=01 remains
        third = services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        # max(existing)=1 -> next is 2, never reusing the deleted 02 -> but here
        # max is 1 so it becomes 2 again (uniqueness still holds because 02 is free).
        self.assertEqual(third.nn, 2)


class UniquenessTests(TestCase):
    def test_duplicate_code_string_rejected(self):
        services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            DocumentCode.objects.create(
                code="QA-FRM-08-06-00-01",  # duplicate string
                section="QA", doctype="FRM", cc="08", ss="06", gg="00", nn=1,
                issue_date=date.today(),
            )

    def test_duplicate_group_serial_rejected(self):
        services.allocate_code(section="QA", doctype="FRM", clause="08-06-00")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            DocumentCode.objects.create(
                code="QA-FRM-08-06-00-01-DUP",  # different string...
                section="QA", doctype="FRM", cc="08", ss="06", gg="00", nn=1,  # ...same group+serial
                issue_date=date.today(),
            )

    def test_allocated_codes_are_all_unique(self):
        seen = {
            services.allocate_code(
                section="STR", doctype="FRM", clause="08-05-00"
            ).code
            for _ in range(25)
        }
        self.assertEqual(len(seen), 25)


class ModuleMappingTests(TestCase):
    def test_module_codes_match_config(self):
        expected = {
            "GATE": "WH-FRM-08-05-00-01",
            "QC": "QA-FRM-08-06-00-01",
            "GRPO": "STR-FRM-08-05-00-01",
        }
        for module, first_code in expected.items():
            doc = services.allocate_for_module(module)
            self.assertEqual(doc.code, first_code)
            self.assertEqual(doc.module, module)

    def test_every_mapped_module_uses_known_section_and_doctype(self):
        from .config import DOCTYPES, SECTIONS

        for module, cfg in MODULE_DOCUMENT_TYPES.items():
            self.assertIn(cfg["section"], SECTIONS, module)
            self.assertIn(cfg["doctype"], DOCTYPES, module)
            numbering.split_clause(cfg["clause"])  # valid clause


class ModelGuardTests(TestCase):
    """The mixin refuses to create a controlled record without a code."""

    def _new_gate_attachment(self, **kwargs):
        # GateAttachment inherits ControlledDocumentMixin; build one in memory.
        from gate_core.models import GateAttachment

        return GateAttachment(gate_entry_id=1, **kwargs)

    def test_saving_without_code_is_refused(self):
        from django.core.exceptions import ValidationError

        att = self._new_gate_attachment()
        with self.assertRaises(ValidationError):
            att.save()

    def test_clean_flags_missing_code(self):
        from django.core.exceptions import ValidationError

        att = self._new_gate_attachment()
        with self.assertRaises(ValidationError):
            att.clean()

    def test_code_string_helpers_null_safe_without_code(self):
        att = self._new_gate_attachment()
        self.assertEqual(att.document_code_str, "")
        self.assertEqual(att.revision_label, "")
        self.assertEqual(att.issue_date_display, "")


class RevisionTests(TestCase):
    def test_revision_bump_keeps_code(self):
        doc = services.allocate_for_module("GRPO")
        original_code = doc.code
        doc.bump_revision(issue_date=date(2026, 7, 23))
        doc.refresh_from_db()
        self.assertEqual(doc.code, original_code)  # code never changes
        self.assertEqual(doc.revision_number, 1)
        self.assertEqual(doc.revision_label, "01")
        self.assertEqual(doc.issue_date_display, "23-07-2026")

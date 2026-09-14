"""
Tests for the label and carton artwork register.

What is worth protecting here is not arithmetic -- there is none -- but the
four rules the register exists to keep:

* a record can never exist without both files;
* only a SAP LABEL or CARTON item can carry artwork;
* a revision never destroys what it replaces;
* a viewer can read everything and change nothing.

SAP is stubbed throughout. The reader is covered by the query it issues, which
was verified against all three live schemas (see ``constants``); what these
tests are for is the behaviour built on top of it, including how the page
behaves when SAP cannot be reached at all.
"""

from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError

from .models import ArtworkRecord, ArtworkRevision

User = get_user_model()

ITEMS_URL = "/api/v1/artwork/items/"
RECORDS_URL = "/api/v1/artwork/records/"

#: What the stubbed SAP item master returns. Real codes and names, taken from
#: the live Oil schema, so the fixtures read like the thing being modelled.
SAP_ITEMS = [
    {
        "item_code": "PM0000086",
        "item_name": "CARTON 1 LTR 10 PCS",
        "sub_group": "CARTON",
        "uom": "PCS",
    },
    {
        "item_code": "PM0000411",
        "item_name": "CARTON 1 LTR 20 PCS PET 26GM",
        "sub_group": "CARTON",
        "uom": "PCS",
    },
    {
        "item_code": "PM0000700",
        "item_name": "LABEL 1 LTR MUSTARD OIL",
        "sub_group": "LABEL",
        "uom": "PCS",
    },
]


def _pdf(name="artwork.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 fake", content_type="application/pdf")


def _cdr(name="artwork.cdr"):
    return SimpleUploadedFile(
        name, b"CDR fake source", content_type="application/octet-stream"
    )


def _user(company, codenames):
    """A user holding exactly the named ``artwork`` permissions."""
    count = User.objects.count()
    user = User.objects.create_user(
        email=f"art{count}@t.com", password="x", full_name=f"Artwork User {count}"
    )
    role = UserRole.objects.create(name=f"R{UserRole.objects.count()}")
    UserCompany.objects.create(user=user, company=company, role=role, is_active=True)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="artwork", codename__in=codenames
        )
    )
    # Re-fetched so the permission cache is not the empty one from creation.
    return User.objects.get(pk=user.pk)


def _client(user, company):
    client = APIClient()
    client.force_authenticate(user=user)
    client.credentials(HTTP_COMPANY_CODE=company.code)
    return client


class _StubReader:
    """Stands in for :class:`artwork.hana_reader.ArtworkItemReader`."""

    items = SAP_ITEMS

    def __init__(self, company_code):
        self.company_code = company_code

    def list_artwork_items(self, *, sub_group=None, search="", limit=2000):
        rows = self.items
        if sub_group:
            rows = [r for r in rows if r["sub_group"] == sub_group.upper()]
        term = (search or "").strip().upper()
        if term:
            rows = [
                r
                for r in rows
                if term in r["item_code"].upper() or term in r["item_name"].upper()
            ]
        return list(rows)

    def get_item(self, item_code):
        for row in self.items:
            if row["item_code"].upper() == (item_code or "").strip().upper():
                return row
        return None


class ArtworkTestBase(APITestCase):
    """A company, an editor and a viewer, with SAP stubbed for the whole class."""

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.editor = _user(self.company, ["can_view_artwork", "can_manage_artwork"])
        self.viewer = _user(self.company, ["can_view_artwork"])
        self.outsider = _user(self.company, [])
        self.client = _client(self.editor, self.company)
        self.viewer_client = _client(self.viewer, self.company)
        self.outsider_client = _client(self.outsider, self.company)

        patcher = patch("artwork.services.ArtworkItemReader", _StubReader)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _capture(self, client=None, **overrides):
        payload = {
            "item_code": "PM0000086",
            "document_number": "JW-CTN-004",
            "revision_number": 0,
            "revision_date": "2026-08-12",
            "barcode": "8906104570123",
            "pdf_file": _pdf(),
            "cdr_file": _cdr(),
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return (client or self.client).post(
            RECORDS_URL, payload, format="multipart"
        )


class CaptureTests(ArtworkTestBase):
    def test_an_editor_files_artwork_against_a_sap_item(self):
        response = self._capture()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        record = ArtworkRecord.objects.get(id=response.data["id"])
        self.assertEqual(record.item_code, "PM0000086")
        # The name and kind come from SAP, not from the request body.
        self.assertEqual(record.item_name, "CARTON 1 LTR 10 PCS")
        self.assertEqual(record.sub_group, "CARTON")
        self.assertEqual(record.barcode, "8906104570123")
        self.assertTrue(record.pdf_file)
        self.assertTrue(record.cdr_file)
        self.assertEqual(response.data["revision_label"], "00")

    def test_both_files_are_required(self):
        without_pdf = self._capture(pdf_file=None)
        self.assertEqual(without_pdf.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pdf_file", without_pdf.data)

        without_cdr = self._capture(cdr_file=None)
        self.assertEqual(without_cdr.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cdr_file", without_cdr.data)

        self.assertFalse(ArtworkRecord.objects.exists())

    def test_the_source_file_must_actually_be_a_cdr(self):
        response = self._capture(cdr_file=_pdf("source.pdf"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cdr", str(response.data).lower())
        self.assertFalse(ArtworkRecord.objects.exists())

    def test_an_item_that_is_not_a_label_or_carton_is_refused(self):
        # A real packaging item, but sub-group CAPS: it carries no artwork.
        response = self._capture(item_code="PM0000121")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("item_code", response.data)
        self.assertFalse(ArtworkRecord.objects.exists())

    def test_an_item_can_only_have_one_live_artwork(self):
        self.assertEqual(self._capture().status_code, status.HTTP_201_CREATED)
        again = self._capture(document_number="JW-CTN-009")
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("item_code", again.data)

    def test_a_document_number_identifies_one_artwork(self):
        self.assertEqual(self._capture().status_code, status.HTTP_201_CREATED)
        clash = self._capture(item_code="PM0000411")
        self.assertEqual(clash.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_number", clash.data)

    def test_capture_is_refused_while_sap_is_unreachable(self):
        """The kind cannot be confirmed, so the record is not filed under a guess."""
        with patch(
            "artwork.services.ArtworkItemReader",
            side_effect=SAPConnectionError("SAP is down"),
        ):
            response = self._capture()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("item_code", response.data)
        self.assertFalse(ArtworkRecord.objects.exists())

    def test_the_barcode_is_optional_so_a_plain_carton_can_be_filed(self):
        response = self._capture(barcode="")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(ArtworkRecord.objects.get().barcode, "")


class ReviseTests(ArtworkTestBase):
    def setUp(self):
        super().setUp()
        self.record = ArtworkRecord.objects.get(id=self._capture().data["id"])
        self.detail_url = f"{RECORDS_URL}{self.record.id}/"

    def test_a_revision_keeps_the_artwork_it_replaces(self):
        original_pdf = self.record.pdf_file.name

        response = self.client.patch(
            self.detail_url,
            {
                "revision_number": 1,
                "revision_date": "2026-09-10",
                "pdf_file": _pdf("artwork-rev1.pdf"),
                "cdr_file": _cdr("artwork-rev1.cdr"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.record.refresh_from_db()
        self.assertEqual(self.record.revision_number, 1)
        self.assertEqual(str(self.record.revision_date), "2026-09-10")
        self.assertNotEqual(self.record.pdf_file.name, original_pdf)

        history = ArtworkRevision.objects.filter(record=self.record)
        self.assertEqual(history.count(), 1)
        superseded = history.first()
        self.assertEqual(superseded.revision_number, 0)
        # The superseded row still points at the file as it was, so the old
        # artwork stays downloadable.
        self.assertEqual(superseded.pdf_file.name, original_pdf)

    def test_correcting_a_barcode_does_not_demand_the_files_again(self):
        response = self.client.patch(
            self.detail_url, {"barcode": "8906104570444"}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.record.refresh_from_db()
        self.assertEqual(self.record.barcode, "8906104570444")
        self.assertTrue(self.record.pdf_file)
        self.assertTrue(self.record.cdr_file)
        # Still recorded: the old barcode is what somebody will come looking for.
        self.assertEqual(
            ArtworkRevision.objects.get(record=self.record).barcode, "8906104570123"
        )

    def test_retiring_frees_the_item_and_keeps_the_history(self):
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)
        self.assertEqual(ArtworkRevision.objects.filter(record=self.record).count(), 1)

        # The item goes back to PENDING and can be captured again.
        again = self._capture()
        self.assertEqual(again.status_code, status.HTTP_201_CREATED, again.data)

    def test_the_history_endpoint_lists_superseded_states(self):
        self.client.patch(
            self.detail_url, {"revision_number": 1}, format="multipart"
        )
        response = self.viewer_client.get(f"{self.detail_url}revisions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["revision_label"], "00")


class ItemListTests(ArtworkTestBase):
    def test_every_item_is_listed_captured_or_not(self):
        self._capture()
        response = self.client.get(ITEMS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(response.data["summary"]["total"], 3)
        self.assertEqual(response.data["summary"]["captured"], 1)
        self.assertEqual(response.data["summary"]["pending"], 2)

        by_code = {row["item_code"]: row for row in response.data["rows"]}
        self.assertEqual(by_code["PM0000086"]["status"], "CAPTURED")
        self.assertEqual(by_code["PM0000086"]["document_number"], "JW-CTN-004")
        self.assertTrue(by_code["PM0000086"]["has_pdf"])
        self.assertTrue(by_code["PM0000086"]["has_cdr"])
        self.assertEqual(by_code["PM0000700"]["status"], "PENDING")
        self.assertIsNone(by_code["PM0000700"]["record_id"])

    def test_the_list_filters_by_kind_and_by_status(self):
        self._capture()

        labels = self.client.get(ITEMS_URL, {"sub_group": "LABEL"})
        self.assertEqual(
            [row["item_code"] for row in labels.data["rows"]], ["PM0000700"]
        )

        pending = self.client.get(ITEMS_URL, {"status": "PENDING"})
        self.assertEqual(
            sorted(row["item_code"] for row in pending.data["rows"]),
            ["PM0000411", "PM0000700"],
        )

    def test_the_search_reaches_the_document_number_and_the_barcode(self):
        """Half the searchable fields exist only in the register, not in SAP.

        Pushing the term into the SAP query would find neither, and would make
        the matching item look as though the item master had dropped it.
        """
        self._capture()

        by_document = self.client.get(ITEMS_URL, {"search": "JW-CTN-004"})
        self.assertEqual(
            [row["item_code"] for row in by_document.data["rows"]], ["PM0000086"]
        )
        self.assertTrue(by_document.data["rows"][0]["in_sap"])

        by_barcode = self.client.get(ITEMS_URL, {"search": "8906104570123"})
        self.assertEqual(
            [row["item_code"] for row in by_barcode.data["rows"]], ["PM0000086"]
        )
        self.assertTrue(by_barcode.data["rows"][0]["in_sap"])

    def test_the_search_still_matches_the_item_itself(self):
        response = self.client.get(ITEMS_URL, {"search": "MUSTARD"})
        self.assertEqual(
            [row["item_code"] for row in response.data["rows"]], ["PM0000700"]
        )

    def test_an_item_sap_no_longer_carries_is_flagged_not_dropped(self):
        self._capture()
        with patch.object(_StubReader, "items", [SAP_ITEMS[1], SAP_ITEMS[2]]):
            response = self.client.get(ITEMS_URL)

        by_code = {row["item_code"]: row for row in response.data["rows"]}
        self.assertIn("PM0000086", by_code)
        self.assertFalse(by_code["PM0000086"]["in_sap"])
        self.assertEqual(by_code["PM0000086"]["status"], "CAPTURED")

    def test_artwork_stays_readable_when_sap_is_unreachable(self):
        self._capture()
        with patch(
            "artwork.services.ArtworkItemReader",
            side_effect=SAPConnectionError("SAP is down"),
        ):
            response = self.client.get(ITEMS_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["sap_available"])
        self.assertTrue(response.data["sap_error"])
        # The captured artwork is still there; only the gaps are unknowable.
        self.assertEqual(
            [row["item_code"] for row in response.data["rows"]], ["PM0000086"]
        )

    def test_an_unknown_sub_group_is_refused_rather_than_ignored(self):
        response = self.client.get(ITEMS_URL, {"sub_group": "CAPS"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class DownloadTests(ArtworkTestBase):
    def setUp(self):
        super().setUp()
        self.record = ArtworkRecord.objects.get(id=self._capture().data["id"])

    def test_a_viewer_can_open_both_files(self):
        base = f"{RECORDS_URL}{self.record.id}/download/"

        pdf = self.viewer_client.get(f"{base}pdf/")
        self.assertEqual(pdf.status_code, status.HTTP_200_OK)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertIn("inline", pdf["Content-Disposition"])

        cdr = self.viewer_client.get(f"{base}cdr/")
        self.assertEqual(cdr.status_code, status.HTTP_200_OK)
        # CorelDRAW has no browser viewer, so it is sent as a download.
        self.assertIn("attachment", cdr["Content-Disposition"])

    def test_an_unknown_file_kind_is_a_clear_refusal(self):
        response = self.viewer_client.get(
            f"{RECORDS_URL}{self.record.id}/download/svg/"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PermissionTests(ArtworkTestBase):
    def test_a_viewer_reads_everything_and_changes_nothing(self):
        record_id = self._capture().data["id"]

        self.assertEqual(
            self.viewer_client.get(ITEMS_URL).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.viewer_client.get(RECORDS_URL).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.viewer_client.get(f"{RECORDS_URL}{record_id}/").status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            self._capture(client=self.viewer_client, item_code="PM0000411").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.viewer_client.patch(
                f"{RECORDS_URL}{record_id}/", {"barcode": "1"}, format="multipart"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.viewer_client.delete(f"{RECORDS_URL}{record_id}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_user_with_no_artwork_permission_sees_nothing(self):
        self.assertEqual(
            self.outsider_client.get(ITEMS_URL).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.outsider_client.get(RECORDS_URL).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_options_tells_the_page_whether_this_user_may_edit(self):
        self.assertTrue(self.client.get("/api/v1/artwork/options/").data["can_manage"])
        self.assertFalse(
            self.viewer_client.get("/api/v1/artwork/options/").data["can_manage"]
        )

    def test_a_record_is_only_readable_from_its_own_company(self):
        record_id = self._capture().data["id"]
        other = Company.objects.create(code="JIVO_MART", name="Jivo Mart")
        intruder = _client(_user(other, ["can_view_artwork"]), other)
        self.assertEqual(
            intruder.get(f"{RECORDS_URL}{record_id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )


class GroupSetupTests(APITestCase):
    def test_the_command_creates_the_viewer_and_editor_groups(self):
        call_command("setup_artwork_groups", verbosity=0)

        viewer = Group.objects.get(name="Artwork Viewer")
        editor = Group.objects.get(name="Artwork Editor")

        self.assertEqual(
            set(viewer.permissions.values_list("codename", flat=True)),
            {"can_view_artwork"},
        )
        self.assertEqual(
            set(editor.permissions.values_list("codename", flat=True)),
            {"can_view_artwork", "can_manage_artwork"},
        )

    def test_running_it_twice_is_safe(self):
        call_command("setup_artwork_groups", verbosity=0)
        call_command("setup_artwork_groups", verbosity=0)
        self.assertEqual(Group.objects.filter(name__startswith="Artwork ").count(), 2)

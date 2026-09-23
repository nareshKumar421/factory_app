"""Uploaded files come back as ABSOLUTE urls.

Django serves media on its own origin and the frontend runs on another in
development (:5173 vs :8000). A bare ``FileField`` serialises to ``/media/...``,
which the browser resolves against the FRONTEND and 404s — which is exactly how
this shipped the first time. Every read of a photo or a bill goes through a
serializer that builds the absolute url from the request, and these tests are
what stop a future view forgetting to pass the request in its context.
"""

import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status

from .base import ConstructionTestCase

#: Uploads land here, not in the developer's real MEDIA_ROOT. Without this the
#: suite leaves a site.png and a bill.png in media/construction/ on every run,
#: in a working tree several agents share.
_TEMP_MEDIA = tempfile.mkdtemp(prefix="construction-test-media-")

# A one-pixel PNG, so the upload is a real image rather than a text file.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@override_settings(MEDIA_ROOT=_TEMP_MEDIA)
class MediaUrlTests(ConstructionTestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TEMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def _log_id(self):
        return self.post(
            f"projects/{self.project.id}/daily-logs/",
            {"log_date": str(self.today), "work_done": "Groundwork started."},
        ).data["log"]["id"]

    def test_photo_url_is_absolute(self):
        log_id = self._log_id()
        response = self.client.post(
            f"/api/v1/construction/daily-logs/{log_id}/photos/",
            {"photo": SimpleUploadedFile("site.png", PNG, content_type="image/png")},
            format="multipart",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        url = response.data["photo"]
        self.assertTrue(
            url.startswith("http://"),
            f"photo url must be absolute so the frontend origin cannot capture it: {url}",
        )
        self.assertIn("/media/construction/daily-logs/", url)

    def test_photo_url_is_absolute_when_read_back(self):
        """The write path and every read path must agree — the day view and the
        log list build their own serializers."""
        log_id = self._log_id()
        self.client.post(
            f"/api/v1/construction/daily-logs/{log_id}/photos/",
            {"photo": SimpleUploadedFile("site.png", PNG, content_type="image/png")},
            format="multipart",
            **self.headers,
        )

        listed = self.get(f"projects/{self.project.id}/daily-logs/").data
        self.assertTrue(listed[0]["photos"][0]["photo"].startswith("http://"))

        detail = self.get(f"daily-logs/{log_id}/").data
        self.assertTrue(detail["photos"][0]["photo"].startswith("http://"))

        day = self.get(f"projects/{self.project.id}/day/", date=str(self.today)).data
        self.assertTrue(day["log"]["photos"][0]["photo"].startswith("http://"))

    def test_bill_url_is_absolute(self):
        response = self.client.post(
            f"/api/v1/construction/projects/{self.project.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "90 bags cement",
                "amount": "31500.00",
                "bill": SimpleUploadedFile("bill.png", PNG, content_type="image/png"),
            },
            format="multipart",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        url = response.data["expense"]["bill"]
        self.assertTrue(url.startswith("http://"), f"bill url must be absolute: {url}")

        listed = self.get(f"projects/{self.project.id}/expenses/").data["results"]
        self.assertTrue(listed[0]["bill"].startswith("http://"))

    def test_an_expense_without_a_bill_reports_none(self):
        """The helper must not turn a missing file into a broken url."""
        created = self.post(
            f"projects/{self.project.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "LABOUR",
                "description": "Mason wages",
                "amount": "7200.00",
            },
        ).data["expense"]
        self.assertIsNone(created["bill"])

    def test_a_log_without_photos_reports_an_empty_list(self):
        self._log_id()
        listed = self.get(f"projects/{self.project.id}/daily-logs/").data
        self.assertEqual(listed[0]["photos"], [])

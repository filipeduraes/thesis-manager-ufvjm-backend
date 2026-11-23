"""Tests for upload deduplication and the in-progress marker."""

from __future__ import annotations

import hashlib
import shutil
import tempfile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.models import StoredFile, Thesis
from theses.services import _store_pdf
from theses.uploads import clear_upload_in_progress, mark_upload_in_progress, upload_cache_key, upload_in_progress

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "upload-tracking-tests"}}
PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


def pdf_upload() -> SimpleUploadedFile:
    """Build a multipart upload with valid PDF magic bytes."""
    return SimpleUploadedFile("monografia.pdf", PDF_BYTES, content_type="application/pdf")


@override_settings(CACHES=TEST_CACHES, UPLOAD_TRACKING_ENABLED=True)
class UploadMarkerTests(SimpleTestCase):
    """The cache marker is set after commit and can be released by the worker."""

    def setUp(self) -> None:
        cache.clear()

    def test_marker_blocks_reupload_until_cleared(self) -> None:
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        self.assertFalse(upload_in_progress(digest))
        mark_upload_in_progress(digest)
        self.assertTrue(upload_in_progress(digest))
        clear_upload_in_progress(digest)
        self.assertFalse(upload_in_progress(digest))

    @override_settings(UPLOAD_TRACKING_ENABLED=False)
    def test_disabled_tracking_does_not_block_uploads(self) -> None:
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        mark_upload_in_progress(digest)
        self.assertFalse(upload_in_progress(digest))


@override_settings(CACHES=TEST_CACHES, UPLOAD_TRACKING_ENABLED=False)
class UploadDeduplicationTests(TestCase):
    """The SHA-256 unique constraint prevents duplicate StoredFile rows."""

    def setUp(self) -> None:
        cache.clear()
        self.media_root = tempfile.mkdtemp(prefix="thesis-upload-tracking-")
        self.override_media = override_settings(MEDIA_ROOT=self.media_root)
        self.override_media.enable()
        self.addCleanup(self.override_media.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)
        department = Department.objects.create(name="Computação")
        self.course = Course.objects.create(department=department, name="SI", code="SI")
        self.user = User.objects.create_user(email="aluno@ufvjm.edu.br", name="Aluno")
        UserProfileRole.objects.create(user=self.user, role=ProfileRole.STUDENT)
        self.advisor = User.objects.create_user(email="orientador@ufvjm.edu.br", name="Orientador")
        UserProfileRole.objects.create(user=self.advisor, role=ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese", status="DRAFT")

    def test_two_uploads_with_the_same_digest_reuse_one_stored_file(self) -> None:
        first = _store_pdf(pdf_upload(), self.thesis, self.user, "THESIS_MAX_PDF_SIZE_MB", 50)
        second = _store_pdf(pdf_upload(), self.thesis, self.user, "THESIS_MAX_PDF_SIZE_MB", 50)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StoredFile.objects.filter(sha256=first.sha256).count(), 1)

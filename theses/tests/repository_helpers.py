"""
Fixtures shared by the public repository tests.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User
from theses.models import Keyword, StoredFile, Thesis, ThesisAuthor, ThesisFile, ThesisKeyword, ThesisStatus
from theses.tests.test_api import TEST_CACHES, build_pdf_bytes, create_user


@dataclass
class ThesisSpec:
    """
    Description of a thesis created directly in the database for repository tests.
    """

    title: str
    course: Course
    advisor: User
    authors: list[User]
    keywords: list[str] = field(default_factory=list)
    abstract: str = "Resumo da monografia."
    status: str = ThesisStatus.PUBLISHED
    defense_date: date | None = None
    published_at: datetime | None = None


def store_pdf(marker: str, owner: User) -> StoredFile:
    """
    Store a real PDF in the test storage and return its ``StoredFile``.

    Args:
        marker: Text that makes the PDF bytes unique.
        owner: Uploading user.

    Returns:
        StoredFile: Persisted file whose digest matches the stored bytes.
    """
    content = build_pdf_bytes(marker)
    stored_name = default_storage.save(f"theses/test/{hashlib.md5(marker.encode()).hexdigest()}.pdf", ContentFile(content))
    return StoredFile.objects.create(
        original_filename=f"{marker[:40]}.pdf",
        stored_filename=stored_name.rsplit("/", 1)[-1],
        size_bytes=len(content),
        sha256=hashlib.sha256(content).digest(),
        storage_path=stored_name,
        uploaded_by=owner,
    )


def create_thesis(spec: ThesisSpec) -> Thesis:
    """
    Create a thesis with authors, keywords and a current PDF.

    Published specs get ``is_public``, ``published_at`` and the defense date/year.

    Args:
        spec: Thesis description.

    Returns:
        Thesis: Created thesis.
    """
    is_published = spec.status == ThesisStatus.PUBLISHED
    defense_date = spec.defense_date or date(2024, 6, 10)
    thesis = Thesis.objects.create(
        course=spec.course,
        advisor=spec.advisor,
        title=spec.title,
        abstract=spec.abstract,
        status=spec.status,
        is_public=is_published,
        published_at=(spec.published_at or timezone.now()) if is_published else None,
        defense_date=defense_date if is_published else None,
        defense_year=defense_date.year if is_published else None,
    )
    for position, author in enumerate(spec.authors, start=1):
        ThesisAuthor.objects.create(thesis=thesis, author=author, position=position, is_main_author=position == 1)
    for keyword_name in spec.keywords:
        keyword, _ = Keyword.objects.get_or_create(normalized_name=keyword_name.casefold(), defaults={"name": keyword_name})
        ThesisKeyword.objects.create(thesis=thesis, keyword=keyword)
    stored_file = store_pdf(f"{spec.title}-{thesis.pk}", spec.authors[0])
    ThesisFile.objects.create(thesis=thesis, stored_file=stored_file, version_number=1, is_current=True, uploaded_by=spec.authors[0])
    return thesis


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class RepositoryTestCase(TestCase):
    """
    Base test case with isolated media storage, a clean cache and two courses.
    """

    def setUp(self) -> None:
        self.media_root = tempfile.mkdtemp(prefix="repository-media-")
        self.override_media = override_settings(MEDIA_ROOT=self.media_root)
        self.override_media.enable()
        self.addCleanup(self.override_media.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)
        # O cache em memória sobrevive entre testes: limpar evita resultados e
        # contadores de throttle de um teste vazando para outro.
        cache.clear()
        self.addCleanup(cache.clear)
        self.department = Department.objects.create(name="Departamento de Computação")
        self.course = Course.objects.create(department=self.department, name="Sistemas de Informação", code="SI")
        self.other_course = Course.objects.create(department=self.department, name="Ciência da Computação", code="CC")
        self.advisor = create_user("orientadora@ufvjm.edu.br", "Ana Orientadora", ProfileRole.ADVISOR)
        self.other_advisor = create_user("bruno@ufvjm.edu.br", "Bruno Professor", ProfileRole.ADVISOR)
        self.student = create_user("carla@ufvjm.edu.br", "Carla Estudante", ProfileRole.STUDENT)
        self.other_student = create_user("diego@ufvjm.edu.br", "Diego Aluno", ProfileRole.STUDENT)
        self.client = APIClient()

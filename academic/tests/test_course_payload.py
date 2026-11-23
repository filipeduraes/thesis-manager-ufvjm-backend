"""Tests of the published-thesis count in the administrative course payload."""

from __future__ import annotations

from datetime import date

from django.test import TestCase

from academic.models import Department
from academic.services import create_course, course_payload
from theses.models import Thesis, ThesisStatus
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis


class CoursePublishedCountTests(RepositoryTestCase):
    """The course payload reports published theses from the thesis service."""

    def test_count_is_computed_from_published_theses(self) -> None:
        course = create_course(name="Computação", code="CCC", department_id=str(self.department.pk))
        create_thesis(ThesisSpec(title="Publicada", course=course, advisor=self.advisor, authors=[self.student], keywords=["teste"], defense_date=date(2024, 6, 10)))
        create_thesis(ThesisSpec(title="Rascunho", course=course, advisor=self.advisor, authors=[self.student], keywords=["teste"], status=ThesisStatus.DRAFT))

        payload = course_payload(course)
        self.assertEqual(payload["publishedTheses"], 1)

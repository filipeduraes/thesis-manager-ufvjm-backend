"""Tests of the global per-user daily upload throttle."""

from __future__ import annotations

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from accounts.models import ProfileRole, User, UserProfileRole
from thesismanagerbackend.throttling import DailyUploadThrottle

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, UPLOAD_GLOBAL_DAILY_LIMIT=2)
class DailyUploadThrottleTests(TestCase):
    """The daily budget is per user and shared across upload endpoints."""

    def setUp(self) -> None:
        cache.clear()
        self.user = User.objects.create_user(email="aluno@ufvjm.edu.br", name="Aluno")
        UserProfileRole.objects.create(user=self.user, role=ProfileRole.STUDENT)
        self.factory = RequestFactory()

    def _request(self):
        request = self.factory.post("/api/v1/theses/me/files")
        request.user = self.user
        return request

    def test_allows_up_to_limit_then_blocks(self) -> None:
        throttle = DailyUploadThrottle()
        self.assertTrue(throttle.allow_request(self._request(), None))
        self.assertTrue(throttle.allow_request(self._request(), None))
        self.assertFalse(throttle.allow_request(self._request(), None))

    def test_budget_is_isolated_per_user(self) -> None:
        throttle = DailyUploadThrottle()
        throttle.allow_request(self._request(), None)
        other = User.objects.create_user(email="outro@ufvjm.edu.br", name="Outro")
        request = self.factory.post("/api/v1/theses/me/files")
        request.user = other
        self.assertTrue(throttle.allow_request(request, None))

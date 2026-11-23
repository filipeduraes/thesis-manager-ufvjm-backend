"""Regression tests: locks must be taken inside a transaction on the owning alias.

With ``SERVICE_DATABASES_SHARED=False`` a ``select_for_update()`` on a model
from the auth/notifications service raises ``TransactionManagementError`` when
the surrounding ``atomic()`` was opened on ``default``. The unit suite runs in
shared mode, so the guard inspects the source of these functions instead.
"""

from __future__ import annotations

import inspect
import textwrap
from typing import Callable

from django.test import SimpleTestCase

from accounts.services import create_external_invitation, create_institutional_user, exchange_external_token, reactivate_user
from accounts.views import AdminUserDetailView, AdminUsersView, _dev_login_user
from academic.services import create_course, delete_course, update_course
from academic.views import AdminDepartmentDetailView, AdminDepartmentsView
from notifications.delivery import retry_pending_notifications


def _source(target: Callable[..., object]) -> str:
    return textwrap.dedent(inspect.getsource(target))


def _assert_explicit_atomic(target: Callable[..., object]) -> None:
    source = _source(target)
    assert "@transaction.atomic" not in source or "@atomic_for_model" in source
    assert "with transaction.atomic():" not in source
    assert "using=" in source or "atomic_for_model" in source


class TransactionAliasTests(SimpleTestCase):
    """Functions that lock rows own an explicit database alias."""

    def test_create_course_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(create_course)

    def test_update_course_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(update_course)

    def test_delete_course_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(delete_course)

    def test_department_patch_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(AdminDepartmentDetailView.patch)

    def test_exchange_external_token_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(exchange_external_token)

    def test_retry_pending_notifications_uses_notifications_alias(self) -> None:
        _assert_explicit_atomic(retry_pending_notifications)

    def test_admin_department_create_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(AdminDepartmentsView.post)

    def test_user_creation_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(create_institutional_user)

    def test_user_reactivation_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(reactivate_user)

    def test_external_invitation_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(create_external_invitation)

    def test_admin_user_create_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(AdminUsersView.post)

    def test_admin_user_patch_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(AdminUserDetailView.patch)

    def test_dev_login_uses_auth_alias(self) -> None:
        _assert_explicit_atomic(_dev_login_user)

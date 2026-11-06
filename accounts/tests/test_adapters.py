"""
Tests for institutional social account adapters.
"""
from __future__ import annotations

from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from accounts.adapters import InstitutionalSocialAccountAdapter

User = get_user_model()


class InstitutionalSocialAccountAdapterTests(TestCase):
    """
    Test Google profile data mapped to the custom user model.
    """
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.adapter = InstitutionalSocialAccountAdapter()
        self.request = self.factory.get("/")

    def test_populate_user_uses_name_from_google_extra_data(self) -> None:
        user = User(email="maria.silva@ufvjm.edu.br")
        account = SocialAccount(
            provider="google",
            uid="google-123",
            extra_data={
                "name": "Maria Silva",
                "email": "maria.silva@ufvjm.edu.br",
            },
        )
        sociallogin = SocialLogin(user=user, account=account)

        populated = self.adapter.populate_user(
            self.request,
            sociallogin,
            {"email": "maria.silva@ufvjm.edu.br"},
        )

        self.assertEqual(populated.name, "Maria Silva")

    def test_existing_user_name_is_synced_from_google_profile(self) -> None:
        user = User.objects.create_user(
            email="maria.silva@ufvjm.edu.br",
            name="maria.silva@ufvjm.edu.br",
        )
        account = SocialAccount(
            provider="google",
            uid="google-123",
            extra_data={
                "name": "Maria Silva",
                "email": "maria.silva@ufvjm.edu.br",
                "email_verified": True,
                "hd": "ufvjm.edu.br",
            },
        )
        sociallogin = SocialLogin(user=user, account=account)

        self.adapter.pre_social_login(self.request, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.name, "Maria Silva")

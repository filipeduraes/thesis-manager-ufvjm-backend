"""
django-allauth adapters enforcing institutional access rules.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect

from accounts.models import ProfileRole, UserProfileRole

logger = logging.getLogger(__name__)


class InstitutionalAccountAdapter(DefaultAccountAdapter):
    """
    Disable local signup, since access is provided by Google Workspace.
    """
    def is_open_for_signup(self, request: HttpRequest) -> bool:
        return False


class InstitutionalSocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Validate Google accounts and provision institutional users.
    """
    def pre_social_login(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        self._validate_social_login(sociallogin)

    def populate_user(self, request: HttpRequest, sociallogin: SocialLogin, data: dict,):
        user = super().populate_user(request, sociallogin, data)
        full_name = (data.get("name") or "").strip()
        if full_name:
            user.name = full_name
        elif not user.name:
            user.name = user.email
        return user

    def save_user(self, request: HttpRequest, sociallogin: SocialLogin, form=None,):
        user = super().save_user(request, sociallogin, form)
        has_student_role = UserProfileRole.objects.filter(
            user=user,
            role=ProfileRole.STUDENT,
            revoked_at__isnull=True,
        ).exists()
        if not has_student_role:
            UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        logger.info("Institutional user provisioned: user_id=%s", user.pk)
        return user

    def is_open_for_signup(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        self._validate_social_login(sociallogin)
        return True

    def _validate_social_login(self, sociallogin: SocialLogin) -> None:
        if sociallogin.account.provider != "google":
            self._reject_login("provedor")

        email = self._extract_email(sociallogin)
        domain = getattr(settings, "INSTITUTIONAL_EMAIL_DOMAIN", "ufvjm.edu.br")
        email_domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if email_domain != domain:
            logger.warning(
                "Rejected social login outside institutional domain: provider=%s",
                sociallogin.account.provider,
            )
            self._reject_login("dominio")

        extra_data = sociallogin.account.extra_data or {}
        verified = extra_data.get("email_verified", extra_data.get("verified_email"))
        if verified is not True:
            logger.warning("Rejected social login with unverified e-mail.")
            self._reject_login("email_nao_verificado")

        hosted_domain = str(extra_data.get("hd") or "").lower()
        if hosted_domain != domain:
            logger.warning("Rejected social login from unexpected hosted domain.")
            self._reject_login("dominio")

    def _extract_email(self, sociallogin: SocialLogin) -> str:
        if sociallogin.user and sociallogin.user.email:
            return sociallogin.user.email.strip().lower()
        for address in sociallogin.email_addresses:
            if address.email:
                return address.email.strip().lower()
        return ""

    def _reject_login(self, reason: str) -> None:
        query = urlencode({"erro": reason})
        response = HttpResponseRedirect(
            f"{settings.FRONTEND_URL.rstrip('/')}/login?{query}"
        )
        raise ImmediateHttpResponse(response)

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
        self._sync_existing_user_name(sociallogin)
        user = sociallogin.user
        if user is not None and user.pk is not None:
            try:
                from academic.services import resolve_pending_coordinators_for_user

                resolve_pending_coordinators_for_user(user, request=request)
            except Exception:
                logger.exception(
                    "Failed to resolve pending coordinators on social login: user_id=%s",
                    user.pk,
                )

    def populate_user(self, request: HttpRequest, sociallogin: SocialLogin, data: dict,):
        user = super().populate_user(request, sociallogin, data)
        full_name = self._extract_full_name(data, sociallogin)
        current_name = (user.name or "").strip()
        if full_name:
            user.name = full_name
        elif not current_name or current_name.lower() == user.email.strip().lower():
            user.name = self._fallback_name(user.email)
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
        # Concede acesso de coordenador quando o e-mail já estiver vinculado a cursos.
        try:
            from academic.services import resolve_pending_coordinators_for_user

            resolve_pending_coordinators_for_user(user, request=request)
        except Exception:
            logger.exception(
                "Failed to resolve pending coordinators on social signup: user_id=%s",
                user.pk,
            )
        logger.info("Institutional user provisioned: user_id=%s", user.pk)
        return user

    def is_open_for_signup(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        self._validate_social_login(sociallogin)
        return True

    def _sync_existing_user_name(self, sociallogin: SocialLogin) -> None:
        """
        Keep an existing user's display name aligned with the Google profile.
        """
        user = sociallogin.user
        if user is None or user.pk is None:
            return
        extra_data = sociallogin.account.extra_data or {}
        full_name = self._extract_full_name(extra_data, sociallogin)
        current_name = (user.name or "").strip()
        if full_name:
            desired_name = full_name
        elif not current_name or current_name.lower() == user.email.lower():
            desired_name = self._fallback_name(user.email)
        else:
            desired_name = current_name
        if desired_name and desired_name != user.name:
            user.name = desired_name
            user.save(update_fields=["name"])

    def _extract_full_name(self, data: dict, sociallogin: SocialLogin) -> str:
        extra_data = sociallogin.account.extra_data or {}
        candidates = [
            data.get("name"),
            extra_data.get("name"),
            " ".join(
                part
                for part in [data.get("first_name"), data.get("last_name")]
                if isinstance(part, str) and part.strip()
            ),
            " ".join(
                part
                for part in [extra_data.get("given_name"), extra_data.get("family_name")]
                if isinstance(part, str) and part.strip()
            ),
        ]
        for candidate in candidates:
            if isinstance(candidate, str):
                normalized = " ".join(candidate.split())
                if normalized:
                    return normalized
        return ""

    def _fallback_name(self, email: str) -> str:
        local_part = (email or "").split("@", 1)[0]
        words = local_part.replace(".", " ").replace("_", " ").replace("-", " ").split()
        return " ".join(word.capitalize() for word in words) or email

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

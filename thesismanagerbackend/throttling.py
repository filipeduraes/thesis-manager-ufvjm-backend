"""
Rate limiting que identifica clientes anônimos pela mesma regra de IP do restante do sistema.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.throttling import BaseThrottle, ScopedRateThrottle

from accounts.network import get_client_ip


class ClientIpScopedRateThrottle(ScopedRateThrottle):
    """
    Scoped throttle whose anonymous identity comes from ``accounts.network.get_client_ip``.

    DRF's ``NUM_PROXIES`` trusts ``X-Forwarded-For`` from any peer. This class
    honors the header only when the peer is in ``TRUSTED_PROXY_CIDRS`` and
    ``TRUSTED_PROXY_COUNT`` is set, so a forged header cannot open new buckets.
    Authenticated users keep being identified by their primary key.
    """

    def get_ident(self, request: Request) -> str:
        """
        Return the client IP used as the anonymous cache key.

        Args:
            request: Incoming DRF request.

        Returns:
            str: Trusted client IP, or ``REMOTE_ADDR`` when it cannot be derived.
        """
        client_ip: str | None = get_client_ip(request._request)
        if client_ip:
            return client_ip
        return request.META.get("REMOTE_ADDR", "")


class DailyUploadThrottle(BaseThrottle):
    """
    Global per-user daily cap across every file-upload endpoint.

    The counter lives in the shared cache (Redis in production) and resets at
    local midnight. External invitation sessions are identified by invitation id.
    """

    def allow_request(self, request: Request, view) -> bool:
        """Consume one slot of the caller's daily upload budget."""
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return True
        principal = getattr(user, "pk", None) or getattr(getattr(user, "invitation", None), "pk", None)
        if principal is None:
            return True
        limit = getattr(settings, "UPLOAD_GLOBAL_DAILY_LIMIT", 50)
        now = timezone.localtime()
        key = f"throttle:upload_global:{principal}:{now.date().isoformat()}"
        current = cache.get(key, 0)
        if current >= limit:
            return False
        seconds_to_midnight = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
        cache.set(key, current + 1, timeout=seconds_to_midnight)
        return True

    def wait(self) -> int:
        """Return a fixed coarse wait in seconds for throttled responses."""
        return 86400

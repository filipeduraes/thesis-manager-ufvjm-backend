"""
Rate limiting that identifies anonymous clients with the same IP rule as the rest of the system.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.throttling import BaseThrottle, ScopedRateThrottle

# Os limitadores são importados pelo DRF junto com as views; manter os tipos
# apenas para checagem evita ciclo em tempo de inicialização.
if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView

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

    def allow_request(self, request: Request, view: APIView | None) -> bool:
        """Consume one slot of the caller's daily upload budget."""
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return True
        principal = getattr(user, "pk", None) or getattr(getattr(user, "invitation", None), "pk", None)
        if principal is None:
            return True
        limit = getattr(settings, "UPLOAD_GLOBAL_DAILY_LIMIT", 50)
        if limit <= 0:
            return False
        now = timezone.localtime()
        key = f"throttle:upload_global:{principal}:{now.date().isoformat()}"
        seconds_to_midnight = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
        # add/incr são atômicos no Redis; um get + set permitiria que várias
        # requisições simultâneas lessem o mesmo contador e excedessem o limite.
        if cache.add(key, 1, timeout=seconds_to_midnight):
            return True
        try:
            current = cache.incr(key)
        except ValueError:
            if cache.add(key, 1, timeout=seconds_to_midnight):
                return True
            current = cache.incr(key)
        return current <= limit

    def wait(self) -> int:
        """Return a fixed coarse wait in seconds for throttled responses."""
        return 86400

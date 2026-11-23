"""
Rate limiting que identifica clientes anônimos pela mesma regra de IP do restante do sistema.
"""
from __future__ import annotations

from rest_framework.request import Request
from rest_framework.throttling import ScopedRateThrottle

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

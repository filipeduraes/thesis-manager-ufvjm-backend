"""
Network classification helpers based on trusted configuration.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

from django.conf import settings
from django.http import HttpRequest

from accounts.models import NetworkOrigin

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def get_client_ip(request: HttpRequest) -> str | None:
    """
    Return the client IP, trusting forwarding headers only when configured.
    """
    raw_ip: str | None = None
    trusted_proxy_count = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")

    if trusted_proxy_count > 0 and forwarded_for:
        addresses = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if len(addresses) < trusted_proxy_count:
            return None
        raw_ip = addresses[-trusted_proxy_count]
    else:
        raw_ip = request.META.get("REMOTE_ADDR")

    if not raw_ip:
        return None

    try:
        return str(ipaddress.ip_address(raw_ip))
    except ValueError:
        return None


def parse_networks(cidrs: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    Parse configured CIDR strings, ignoring invalid entries.
    """
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return networks


def is_ip_allowed(ip_address: str | None, cidrs: Iterable[str]) -> bool:
    """
    Return whether ``ip_address`` belongs to at least one configured network.
    """
    if not ip_address:
        return False

    networks = parse_networks(cidrs)
    if not networks:
        return False

    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return False

    return any(address in network for network in networks)


def classify_ip(ip_address: str | None) -> NetworkOrigin:
    """
    Classify an IP address as institutional network, VPN or external.
    """
    if is_ip_allowed(ip_address, getattr(settings, "INSTITUTIONAL_NETWORK_CIDRS", [])):
        return NetworkOrigin.INSTITUTIONAL_NETWORK
    if is_ip_allowed(ip_address, getattr(settings, "VPN_NETWORK_CIDRS", [])):
        return NetworkOrigin.VPN
    return NetworkOrigin.EXTERNAL


def request_origin(request: HttpRequest) -> NetworkOrigin:
    """
    Classify the origin of an HTTP request.
    """
    cached_origin = getattr(request, "network_origin", None)
    if isinstance(cached_origin, NetworkOrigin):
        return cached_origin
    return classify_ip(get_client_ip(request))

"""
Independent HTTP APIs of each logical service.

The full URLconf remains the compatibility gateway; each module here exposes
only the routes owned by one service so it can be deployed as its own process
with ``SERVICE_NAME`` set.
"""

from __future__ import annotations

from django.urls import URLPattern, URLResolver

SERVICES: tuple[str, ...] = ("auth", "theses", "committee", "repository", "notifications", "reports")


def patterns_for(app_urls, prefixes: tuple[str, ...] = (), names: tuple[str, ...] = ()) -> list[URLPattern | URLResolver]:
    """
    Select the routes of an app that belong to a service.

    Args:
        app_urls: An app's ``urlpatterns``.
        prefixes: Route prefixes to include.
        names: Route names to include regardless of prefix.

    Returns:
        list: Selected URL patterns.
    """
    return [
        item for item in app_urls
        if any(str(item.pattern).startswith(prefix) for prefix in prefixes)
        or getattr(item, "name", None) in names
    ]

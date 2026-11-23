"""
Shared conversion of query parameters for the thesis API.
"""


def parse_year_filter(value: object) -> int | None:
    """
    Return the year from a query parameter, or ``None`` when invalid.

    Args:
        value: Raw query parameter value.

    Returns:
        int | None: Parsed year, or ``None`` when the value is empty or invalid.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None

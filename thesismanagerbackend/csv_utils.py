"""Shared helpers for CSV exports consumed by spreadsheet applications."""

from __future__ import annotations


def escape_formula_cell(value: object) -> object:
    """
    Neutralize spreadsheet formulas supplied by users in exported cells.

    A leading apostrophe keeps values such as ``=HYPERLINK(...)`` or
    ``@SUM(...)`` as text when an administrator opens the CSV in Excel or
    LibreOffice. Non-string cells (numbers and None) are returned unchanged.

    Args:
        value: Original cell value.

    Returns:
        object: Value safe to pass to ``csv.writer``.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in "=+-@\t\r":
        return "'" + value
    return value

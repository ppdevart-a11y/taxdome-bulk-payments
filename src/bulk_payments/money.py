"""Money is integer cents everywhere; dollar strings exist only at the API edge.

Floats never touch an amount: ``float("0.1") * 100`` is ``10.000000000000002``.
"""

import re

# ASCII digits only: `\d` also matches e.g. Arabic-Indic digits, which int()
# would happily convert. At most 15 integer digits keeps any single amount far
# below BIGINT, and fullmatch() (unlike `$`) rejects a trailing newline.
_AMOUNT_RE = re.compile(r"(?P<dollars>[0-9]{1,15})(?:\.(?P<cents>[0-9]{1,2}))?")


def parse_amount(value: str) -> int:
    """Convert a positive dollar amount like ``"1200.75"`` or ``"300"`` to cents."""
    match = _AMOUNT_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            "must be a positive number of US dollars, with at most 15 digits before the "
            'decimal point and 2 after, e.g. "1200.75" or "300"'
        )
    cents = int(match["dollars"]) * 100 + int((match["cents"] or "0").ljust(2, "0"))
    if cents <= 0:
        raise ValueError("must be greater than zero")
    return cents


def format_cents(cents: int) -> str:
    """Render cents as a dollar string with exactly two decimals: 580050 -> "5800.50"."""
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{remainder:02d}"

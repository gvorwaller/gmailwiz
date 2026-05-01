"""Category enum and Gmail label-name mapping.

Phase 1 only *reads* and classifies. The label name mapping is defined here so
Phase 2 can wire `gmailwiz/<category>` labels without re-deciding names. We
emit no labels in Phase 1.
"""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    """Sender category. Mirrors the strict JSON output the classifier returns.

    `unknown` is the explicit fallback for senders whose classification failed
    or returned an out-of-vocabulary value. Phase 2 must exclude `unknown`
    senders from any mutation plan (per implementation plan).
    """

    PROMOTIONAL = "promotional"
    TRANSACTIONAL = "transactional"
    NEWSLETTER = "newsletter"
    PERSONAL = "personal"
    UNKNOWN = "unknown"


# The four categories the classifier is allowed to return. `unknown` is reserved
# for parse failures / refusal — never a model-supplied output.
CLASSIFIABLE_CATEGORIES: tuple[Category, ...] = (
    Category.PROMOTIONAL,
    Category.TRANSACTIONAL,
    Category.NEWSLETTER,
    Category.PERSONAL,
)


def gmail_label_name(category: Category) -> str:
    """Return the Gmail label that Phase 2 will apply for the given category."""
    return f"gmailwiz/{category.value}"


def parse_category(value: str) -> Category:
    """Parse a string into a `Category`, returning `UNKNOWN` for any unrecognised value.

    The classifier and CLI both call this so that bad input never silently
    coerces to an arbitrary category.
    """
    if not isinstance(value, str):
        return Category.UNKNOWN
    try:
        return Category(value.strip().lower())
    except ValueError:
        return Category.UNKNOWN

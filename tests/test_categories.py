"""Tests for `gmailwiz.categories.parse_category` and label naming."""

from __future__ import annotations

import pytest

from gmailwiz.categories import Category, gmail_label_name, parse_category


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("promotional", Category.PROMOTIONAL),
        ("transactional", Category.TRANSACTIONAL),
        ("newsletter", Category.NEWSLETTER),
        ("personal", Category.PERSONAL),
        ("unknown", Category.UNKNOWN),
        ("PROMOTIONAL", Category.PROMOTIONAL),
        ("  Personal  ", Category.PERSONAL),
    ],
)
def test_parse_category_valid(raw, expected):
    assert parse_category(raw) is expected


@pytest.mark.parametrize(
    "bad",
    ["", "promo", "spam", "important", None, 42, [], {}],
)
def test_parse_category_invalid_returns_unknown(bad):
    assert parse_category(bad) is Category.UNKNOWN


def test_gmail_label_name_format():
    """Phase 2 will apply these label names — lock the format."""
    assert gmail_label_name(Category.PROMOTIONAL) == "gmailwiz/promotional"
    assert gmail_label_name(Category.NEWSLETTER) == "gmailwiz/newsletter"
    assert gmail_label_name(Category.UNKNOWN) == "gmailwiz/unknown"

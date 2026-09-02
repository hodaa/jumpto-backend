"""Unit tests for language normalization helpers."""

from app.services.language import normalize_language


def test_normalize_english_locales() -> None:
    """English locales normalize to 'en'."""
    assert normalize_language("en") == "en"
    assert normalize_language("en-us") == "en"
    assert normalize_language("en-US") == "en"
    assert normalize_language("eng") == "en"


def test_normalize_arabic_locales() -> None:
    """Arabic locales normalize to 'ar'."""
    assert normalize_language("ar") == "ar"
    assert normalize_language("ar-EG") == "ar"
    assert normalize_language("ar-SA") == "ar"
    assert normalize_language("ara") == "ar"


def test_normalize_unknown_and_empty() -> None:
    """Unknown or empty inputs normalize to None."""
    assert normalize_language(None) is None
    assert normalize_language("") is None
    assert normalize_language("  ") is None
    assert normalize_language("fr") is None
    assert normalize_language("zh-CN") is None
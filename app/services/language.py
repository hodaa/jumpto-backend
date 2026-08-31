"""Language normalization and matching helpers."""

from functools import lru_cache

# Supported two-letter language codes and their accepted aliases.
_SUPPORTED: dict[str, set[str]] = {
    "en": {"en", "eng"},
    "ar": {"ar", "ara"},
}


@lru_cache(maxsize=256)
def normalize_language(detected: str | None) -> str | None:
    """
    Normalize a detected language/locale to a supported two-letter code.

    Examples: "en" -> "en", "en-us" -> "en", "ar-EG" -> "ar".

    Args:
        detected: Language code returned by the transcript provider

    Returns:
        Supported two-letter code, or None when unrecognized
    """
    if not detected:
        return None
    base = detected.strip().split("-")[0].split("_")[0].lower()
    code = _base_to_code(base)
    return code if code in _SUPPORTED else None


@lru_cache(maxsize=16)
def languages_match(selected: str | None, detected: str | None) -> bool:
    """
    Return whether a user-selected language matches a detected language.

    Args:
        selected: User-selected two-letter language code
        detected: Language code detected from the transcript

    Returns:
        True when both normalize to the same supported language
    """
    if not selected or not detected:
        return False
    return normalize_language(selected) == normalize_language(detected)


def _base_to_code(base: str) -> str:
    """Map an ISO-639 base code to a supported code, defaulting to itself."""
    for code, aliases in _SUPPORTED.items():
        if base == code or base in aliases:
            return code
    return base

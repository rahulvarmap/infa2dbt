"""Shared identifier matching rules for PowerCenter names."""


def normalize_identifier(value: str) -> str:
    """Compare identifiers with locale-independent Unicode lowercase matching."""
    return value.lower()


def normalize_type_name(value: str) -> str:
    """Normalize transformation type labels without changing identifier spelling."""
    return " ".join(normalize_identifier(value).replace("_", " ").replace("-", " ").split())

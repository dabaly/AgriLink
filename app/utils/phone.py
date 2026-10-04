"""Phone number normalization and validation."""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


def normalize_phone(value: str) -> str:
    """Parse a phone number (Kenya is the default region) and return E.164."""
    cleaned = value.strip()
    try:
        number = phonenumbers.parse(cleaned, "KE")
    except NumberParseException as exc:
        raise ValueError("Invalid phone number") from exc
    if not phonenumbers.is_valid_number(number):
        raise ValueError("Invalid phone number")
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)

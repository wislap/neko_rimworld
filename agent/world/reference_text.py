"""Text accepted for model-supplied ids, names, definitions and map ids."""

MAX_REFERENCE_TEXT = 256


def is_reference_text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_REFERENCE_TEXT

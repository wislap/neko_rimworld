"""Primitive JSON value predicates shared by the game and workspace layers."""


def is_nonblank_text(value):
    return isinstance(value, str) and bool(value.strip())

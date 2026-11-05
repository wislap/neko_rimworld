"""Lazy SDK entry; importing the domain package does not load its host facade."""

__all__ = ["NekoRimworldPlugin"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    from . import entry

    return getattr(entry, name)


def __dir__():
    return sorted(set(globals()) | set(__all__))

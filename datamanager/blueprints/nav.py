from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    """A sidebar entry, declared by the blueprint that owns the section."""

    label: str
    endpoint: str
    blueprint: str
    order: int = 100
    position: str = "top"  # "top" | "bottom"
    badge: Callable[[], str | None] | None = None  # returns a warning text when the section needs attention

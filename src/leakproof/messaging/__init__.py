"""Registered-template-only customer messaging types."""

from leakproof.messaging.templates import (
    MissingVariable,
    RenderedMessage,
    TemplateRegistry,
    UndeclaredVariable,
    UnknownTemplate,
    UnsupportedLanguage,
)

__all__ = [
    "MissingVariable",
    "RenderedMessage",
    "TemplateRegistry",
    "UndeclaredVariable",
    "UnknownTemplate",
    "UnsupportedLanguage",
]

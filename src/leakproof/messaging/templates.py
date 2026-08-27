from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from leakproof.config import TemplateConfig, get_policy_config


class TemplateError(ValueError):
    """Base class for messages rejected by the registry."""


class UnknownTemplate(TemplateError):
    pass


class MissingVariable(TemplateError):
    pass


class UndeclaredVariable(TemplateError):
    pass


class UnsupportedLanguage(TemplateError):
    pass


@dataclass(frozen=True, init=False)
class RenderedMessage:
    """Opaque actuator input proving content came from a registered template."""

    template_id: str
    channel: str
    registration_ref: str
    language: str
    tone: str
    body: str
    variables: Mapping[str, Any]

    @classmethod
    def _from_registry(
        cls,
        *,
        template_id: str,
        channel: str,
        registration_ref: str,
        language: str,
        tone: str,
        body: str,
        variables: dict[str, Any],
    ) -> RenderedMessage:
        instance = object.__new__(cls)
        object.__setattr__(instance, "template_id", template_id)
        object.__setattr__(instance, "channel", channel)
        object.__setattr__(instance, "registration_ref", registration_ref)
        object.__setattr__(instance, "language", language)
        object.__setattr__(instance, "tone", tone)
        object.__setattr__(instance, "body", body)
        object.__setattr__(instance, "variables", MappingProxyType(dict(variables)))
        return instance


class TemplateRegistry:
    def __init__(self, templates: list[TemplateConfig] | None = None) -> None:
        configured = templates if templates is not None else get_policy_config().templates
        self._templates = {template.id: template for template in configured}
        if len(self._templates) != len(configured):
            raise ValueError("template ids must be unique")

    def render(
        self,
        template_id: str,
        variables: dict[str, Any],
        *,
        language: str = "en-IN",
    ) -> RenderedMessage:
        template = self._templates.get(template_id)
        if template is None:
            raise UnknownTemplate(template_id)

        declared = set(template.variables)
        supplied = set(variables)
        missing = declared - supplied
        undeclared = supplied - declared
        if missing:
            raise MissingVariable(", ".join(sorted(missing)))
        if undeclared:
            raise UndeclaredVariable(", ".join(sorted(undeclared)))
        if language not in template.languages or language not in template.content:
            raise UnsupportedLanguage(language)

        values = dict(variables)
        return RenderedMessage._from_registry(
            template_id=template.id,
            channel=template.channel,
            registration_ref=template.dlt_or_meta_ref,
            language=language,
            tone=template.tone,
            body=template.content[language].format_map(values),
            variables=values,
        )

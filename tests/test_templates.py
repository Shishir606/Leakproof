from __future__ import annotations

import pytest

from leakproof.messaging import (
    MissingVariable,
    RenderedMessage,
    TemplateRegistry,
    UndeclaredVariable,
    UnknownTemplate,
    UnsupportedLanguage,
)


@pytest.fixture
def variables():
    return {
        "payer_name": "Asha",
        "invoice_no": "INV-27",
        "amount": "INR 1,000",
        "due_date": "27 Aug 2026",
        "link": "https://pay.example/registered",
    }


def test_registry_returns_the_only_message_type_allowed_at_the_actuator_boundary(variables):
    message = TemplateRegistry().render(
        "util_invoice_reminder_v3", variables, language="hinglish"
    )

    assert isinstance(message, RenderedMessage)
    assert message.channel == "whatsapp"
    assert message.registration_ref == "1007xxxxxxxxxxxx"
    assert "Asha" in message.body
    with pytest.raises(TypeError):
        message.variables["payer_name"] = "changed"
    with pytest.raises(TypeError):
        RenderedMessage(
            "made_up", "whatsapp", "none", "en-IN", "unsafe", "free text", {}
        )


def test_registry_rejects_unknown_templates(variables):
    with pytest.raises(UnknownTemplate):
        TemplateRegistry().render("model_generated_free_text", variables)


def test_registry_rejects_missing_variables(variables):
    variables.pop("link")
    with pytest.raises(MissingVariable, match="link"):
        TemplateRegistry().render("util_invoice_reminder_v3", variables)


def test_registry_rejects_undeclared_variables(variables):
    variables["threat"] = "pay now or else"
    with pytest.raises(UndeclaredVariable, match="threat"):
        TemplateRegistry().render("util_invoice_reminder_v3", variables)


def test_registry_rejects_an_unregistered_language(variables):
    with pytest.raises(UnsupportedLanguage):
        TemplateRegistry().render("util_invoice_reminder_v3", variables, language="fr-FR")

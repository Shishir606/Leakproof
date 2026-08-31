import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "verify_foundation", Path(__file__).parents[1] / "scripts" / "verify_foundation.py"
)
assert SPEC is not None and SPEC.loader is not None
verify_foundation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_foundation)
assert_semantic_sequence = verify_foundation.assert_semantic_sequence
safe_error_summary = verify_foundation.safe_error_summary


def test_foundation_sequence_includes_assignment_and_two_unique_signals():
    assert_semantic_sequence(["DETECTED", "ASSIGNED", "SIGNAL", "SIGNAL"])

    with pytest.raises(AssertionError, match="semantic event sequence"):
        assert_semantic_sequence(["DETECTED", "SIGNAL", "SIGNAL"])


def test_foundation_timeout_error_summary_redacts_credentials_and_contacts():
    assert safe_error_summary("Bearer private-token failed for reviewer@example.com") == (
        "Bearer [REDACTED] failed for [REDACTED_EMAIL]"
    )
    assert safe_error_summary("api_key=private-value request failed") == (
        "api_key=[REDACTED] request failed"
    )


def test_forced_worker_timeout_contains_actionable_redacted_diagnostics(monkeypatch):
    diagnostics = {
        "inbox_count": 3,
        "processed_count": 2,
        "event_kinds": ["DETECTED", "ASSIGNED", "SIGNAL"],
        "processing_attempts": 4,
        "last_errors": ["provider timeout"],
    }
    ticks = iter([0.0, 1.0, 16.0])
    monkeypatch.setattr(verify_foundation.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(verify_foundation.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        verify_foundation, "webhook_diagnostics", lambda _connection, _run_id: diagnostics
    )

    with pytest.raises(TimeoutError) as raised:
        verify_foundation.wait_for_processed_webhooks(object(), "forced-failure")

    message = str(raised.value)
    assert '"processed_count": 2' in message
    assert '"processing_attempts": 4' in message
    assert "provider timeout" in message

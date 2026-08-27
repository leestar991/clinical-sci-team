"""``thinking_enabled=False`` must actually reach the provider, or say it could not.

Session ``a7c19ea1``: 16 of 54 compaction attempts (30%) returned an empty summary, every
one ``finish_reason='length'`` with ``output_tokens≈8192`` and 13k–25k reasoning chars —
the summariser spent its whole ``max_tokens`` budget thinking and emitted no body. The
middleware had passed ``thinking_enabled=False`` all along
(``summarization_middleware.py``), so the setting looked correct everywhere it was read.

The break is in ``create_chat_model``: every thinking-disable branch is guarded on
``has_thinking_settings`` — "did this model declare *any* thinking config?" — and
``deepseek-v4-flash`` declared none. The flag was a no-op and nothing logged it. The
earlier remedy (``trim_tokens_to_summarize`` 120000 → 40000) therefore treated the wrong
cause: with reasoning uncapped, a smaller input still fills 8192.

Two guarantees pinned here:
  1. A model declaring ``when_thinking_disabled`` gets it applied when thinking is off.
  2. A model declaring nothing produces a WARNING naming the model and its max_tokens,
     so the no-op is visible instead of silent.
"""

import logging

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.models import create_chat_model

_BASE_MODEL: dict = {
    "use": "langchain_openai:ChatOpenAI",
    "model": "some-model",
    "api_key": "test-key",
    "max_tokens": 8192,
}

_THINKING_KEYS = {
    "supports_thinking": True,
    "when_thinking_enabled": {"extra_body": {"thinking": {"type": "enabled"}}},
    "when_thinking_disabled": {"extra_body": {"thinking": {"type": "disabled"}}},
}


def _app_config(**model_overrides) -> AppConfig:
    """An AppConfig holding one model named ``summariser``."""
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            "models": [{"name": "summariser", **_BASE_MODEL, **model_overrides}],
        },
    )


def _extra_body(model) -> dict:
    """The ``extra_body`` the constructed model would send, however it is stored."""
    for attr in ("extra_body", "model_kwargs"):
        value = getattr(model, attr, None)
        if isinstance(value, dict):
            if attr == "model_kwargs":
                nested = value.get("extra_body")
                if isinstance(nested, dict):
                    return nested
                continue
            return value
    return {}


def test_declared_disable_settings_are_applied_when_thinking_is_off():
    """The fix for a7c19ea1: with the keys present, thinking really is turned off."""
    model = create_chat_model(
        name="summariser",
        thinking_enabled=False,
        app_config=_app_config(**_THINKING_KEYS),
        attach_tracing=False,
    )

    assert _extra_body(model).get("thinking") == {"type": "disabled"}


def test_declared_enable_settings_are_applied_when_thinking_is_on():
    """The same model must still be able to think when asked to — no blanket disable."""
    model = create_chat_model(
        name="summariser",
        thinking_enabled=True,
        app_config=_app_config(**_THINKING_KEYS),
        attach_tracing=False,
    )

    assert _extra_body(model).get("thinking") == {"type": "enabled"}


def test_model_without_thinking_settings_warns_that_disabling_is_a_no_op(caplog):
    """The exact a7c19ea1 shape: flag passed, nothing declared, nothing happened.

    Without this warning the empty-summary failures are only diagnosable by correlating
    provider ``finish_reason`` against a config file, which is how the bug survived.
    """
    with caplog.at_level(logging.WARNING, logger="deerflow.models.factory"):
        model = create_chat_model(
            name="summariser",
            thinking_enabled=False,
            app_config=_app_config(),
            attach_tracing=False,
        )

    assert _extra_body(model).get("thinking") is None, "nothing to apply — that is the point"

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a silent no-op is what this test exists to prevent"
    message = "\n".join(warnings)
    assert "summariser" in message, "must name the offending model"
    assert "8192" in message, "must name the budget reasoning can consume"
    assert "when_thinking_disabled" in message, "must name the key that fixes it"


def test_no_warning_when_disable_settings_are_declared(caplog):
    """A correctly configured model must stay quiet, or the warning becomes noise."""
    with caplog.at_level(logging.WARNING, logger="deerflow.models.factory"):
        create_chat_model(
            name="summariser",
            thinking_enabled=False,
            app_config=_app_config(**_THINKING_KEYS),
            attach_tracing=False,
        )

    assert not [r for r in caplog.records if "no thinking settings" in r.getMessage()]


def test_no_warning_when_thinking_is_requested_on(caplog):
    """The warning is about a no-op *disable*; enabling has its own error path."""
    with caplog.at_level(logging.WARNING, logger="deerflow.models.factory"):
        create_chat_model(
            name="summariser",
            thinking_enabled=True,
            app_config=_app_config(),
            attach_tracing=False,
        )

    assert not [r for r in caplog.records if "no thinking settings" in r.getMessage()]


@pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "deepseek-v4-flash-responses"])
def test_repo_summariser_models_declare_disable_settings(model_name):
    """Guard the actual config: the summariser must never regress to declaring nothing.

    ``summarization.model_name`` points at ``deepseek-v4-flash``, and that pairing —
    summariser + no thinking settings + max_tokens 8192 — is precisely what produced the
    empty summaries. Read the repo config rather than a fixture so the check binds to what
    ships.
    """
    from pathlib import Path

    import yaml

    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    if not config_path.exists():  # config.yaml is gitignored; skip on a fresh checkout
        pytest.skip("config.yaml not present (generated from config.example.yaml)")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    models = {m.get("name"): m for m in (raw.get("models") or []) if isinstance(m, dict)}
    if model_name not in models:
        pytest.skip(f"model {model_name} not configured locally")

    entry = models[model_name]
    assert entry.get("when_thinking_disabled"), f"{model_name} is (or backs) the summarisation model; without `when_thinking_disabled` the middleware's thinking_enabled=False is a no-op and reasoning can eat all of max_tokens"

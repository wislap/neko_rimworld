"""Run policy settings round-trip independently of provider credentials."""

import pytest

from ..agent.model.model_client import AgentConfigurationError, build_agent_model_settings, parse_agent_model_settings


def configured(**limits):
    return build_agent_model_settings(base_url="http://127.0.0.1:11434/v1", model="offline", **limits)


def test_default_run_policy_and_explicit_existing_deadline_round_trip():
    policy = configured()
    assert (policy.run_budget_seconds, policy.max_model_calls, policy.max_input_tokens) == (900, 60, 2_000_000)
    restored, errors = parse_agent_model_settings({"neko_rimworld": {"agent": {
        "base_url": policy.base_url, "model": policy.model, "run_budget_seconds": 90,
        "max_model_calls": 7, "max_input_tokens": 12_345,
    }}})
    assert not errors
    assert (restored.run_budget_seconds, restored.max_model_calls, restored.max_input_tokens) == (90, 7, 12_345)
    assert restored.public_dict()["max_input_tokens"] == 12_345


@pytest.mark.parametrize("field", ["max_model_calls", "max_input_tokens"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "60", None])
def test_run_count_and_token_policy_reject_non_positive_or_non_integer_values(field, value):
    with pytest.raises(AgentConfigurationError, match=field):
        configured(**{field: value})


@pytest.mark.parametrize("value", [False, float("nan"), float("inf"), 9, 86_401])
def test_run_time_policy_rejects_invalid_or_unbounded_values(value):
    with pytest.raises(AgentConfigurationError, match="run budget"):
        configured(run_budget_seconds=value)


def test_run_policy_public_view_keeps_credentials_private():
    public = configured(api_key="local-secret", max_model_calls=2).public_dict()
    assert public["max_model_calls"] == 2 and public["api_key_configured"]
    assert "api_key" not in public and "local-secret" not in str(public)

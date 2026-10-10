from pathlib import Path

import pytest

from adp.llm import LLMSettings, load_settings
from adp.llm.runner import AgentSpec


def test_ambient_api_key_is_never_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient")
    monkeypatch.setenv("CLAUDECODE", "1")
    settings = load_settings(tmp_path / ".env")
    assert settings.auth_mode == "claude_code"
    env = settings.subprocess_env()
    assert env["ANTHROPIC_API_KEY"] == ""  # 空にすると CLI はログイン認証を使う
    assert env["CLAUDECODE"] == ""


@pytest.mark.parametrize(
    "var",
    [
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ],
)
def test_other_auth_variables_are_blanked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str
) -> None:
    monkeypatch.setenv(var, "1")
    assert load_settings(tmp_path / ".env").subprocess_env()[var] == ""


def test_dotenv_api_key_and_models(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\nANTHROPIC_API_KEY='sk-ant-from-dotenv'\nADP_DEFAULT_MODEL=claude-x\n",
        encoding="utf-8",
    )
    settings = load_settings(dotenv)
    assert settings.auth_mode == "api_key"
    assert settings.subprocess_env()["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"
    assert settings.default_model == "claude-x"
    assert settings.escalation_model == LLMSettings().escalation_model


def test_allowed_tools_are_only_declared_tools() -> None:
    from claude_agent_sdk import tool

    @tool("t", "d", {})
    async def t(args: dict[str, object]) -> dict[str, object]:
        return {}

    spec = AgentSpec(system_prompt="", model="m", tools={"srv": [t]})
    assert spec.allowed_tools == ["mcp__srv__t"]


def test_usage_log_has_no_content(tmp_path: Path) -> None:
    import json

    from adp.llm import ClaudeAgentRunner, TurnResult

    log = tmp_path / "llm.jsonl"
    runner = ClaudeAgentRunner(LLMSettings(), usage_log=log)
    runner._record(TurnResult("s1", "m", False, 1.234, 0.5, 10, 20, None, 0.01), "interview")
    entry = json.loads(log.read_text())
    assert entry["purpose"] == "interview" and entry["output_tokens"] == 20
    assert entry["auth"] == "claude_code"
    assert set(entry) == {
        "at",
        "purpose",
        "auth",
        "model",
        "session_id",
        "is_error",
        "duration_s",
        "first_token_s",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    }


def test_input_tokens_include_cache() -> None:
    from adp.llm.runner import _input_tokens

    assert (
        _input_tokens(
            {"input_tokens": 2, "cache_creation_input_tokens": 475, "cache_read_input_tokens": 917}
        )
        == 1394
    )
    assert _input_tokens({}) is None


async def test_usage_is_recorded_even_when_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from adp.llm import AgentSpec, ClaudeAgentRunner
    from adp.llm import runner as runner_module

    async def broken_query(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("CLI crashed")
        yield  # pragma: no cover

    monkeypatch.setattr(runner_module, "query", broken_query)
    log = tmp_path / "llm.jsonl"
    runner = ClaudeAgentRunner(LLMSettings(), usage_log=log)
    with pytest.raises(RuntimeError):
        async for _ in runner.run("x", AgentSpec(system_prompt="", model="m", purpose="repair")):
            pass
    entry = json.loads(log.read_text())
    assert entry["is_error"] and entry["purpose"] == "repair"

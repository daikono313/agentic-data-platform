"""LLM の設定と認証の切り替え。

認証は既定で Claude Code のログイン（サブスクリプション）を使う。リポジトリ直下の `.env` に
`ANTHROPIC_API_KEY` を書いたときだけ API キーで動く。シェルの環境変数にあるキーは使わない
（意図せず従量課金にならないようにするため。design-doc R10）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_MODEL = "claude-sonnet-5-5"
ESCALATION_MODEL = "claude-opus-5-5"

# 認証や接続先を切り替える変数。.env で明示しない限り、シェルから引き継がない
# （引き継ぐと、表示上は claude_code のまま従量課金や別の接続先になりうる）
_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# Claude Code の中から起動された場合に引き継がれる変数。子プロセスを独立したセッションにする
_NESTED_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
)


@dataclass(frozen=True)
class LLMSettings:
    default_model: str = DEFAULT_MODEL
    escalation_model: str = ESCALATION_MODEL
    api_key: str | None = None

    @property
    def auth_mode(self) -> str:
        return "api_key" if self.api_key else "claude_code"

    def subprocess_env(self) -> dict[str, str]:
        """Claude Code CLI の子プロセスに渡す環境変数の上書き。

        認証・接続先の変数はすべて空にし（空文字は未設定と同じ扱いになる）、ログイン認証を使わせる。
        .env に API キーがあるときだけ、それを渡す。
        """
        env = {var: "" for var in _NESTED_SESSION_VARS if var in os.environ}
        env |= {var: "" for var in _AUTH_VARS}
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
        return env


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_settings(dotenv: Path | None = None) -> LLMSettings:
    values = read_dotenv(dotenv or REPO_ROOT / ".env")
    return LLMSettings(
        default_model=values.get("ADP_DEFAULT_MODEL") or DEFAULT_MODEL,
        escalation_model=values.get("ADP_ESCALATION_MODEL") or ESCALATION_MODEL,
        api_key=values.get("ANTHROPIC_API_KEY") or None,
    )

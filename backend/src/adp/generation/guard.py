"""生成エージェントの操作範囲を制限する（docs/guardrails.md の 4）。

- ファイル操作（Read / Edit / Write / Glob / Grep）は作業ディレクトリの中だけ（`~` も展開して判定）
- 書き込めるのは dbt の SQL / YAML / Markdown だけ。プロジェクト設定・プロファイル・パッケージ定義・
  Python ファイルは書き換えられない。`env_var(` を含む内容も書けない（秘密情報の持ち出し防止）
- Bash は dbt / mf の決まったサブコマンドと、決まったオプションだけ
- DuckDB の外部アクセスは profiles.yml で無効（read_text / COPY TO などを禁止）
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}
BUILTIN_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
WRITABLE_SUFFIXES = {".sql", ".yml", ".yaml", ".md"}
PROTECTED_FILES = {
    "dbt_project.yml",
    "profiles.yml",
    "packages.yml",
    "package-lock.yml",
    "selectors.yml",
}
_DBT = {"parse", "build", "compile", "test", "ls", "list"}
_MF = {"validate-configs", "query", "list"}
# 値を取るオプション（値にパスを渡せないものだけ）
_DBT_FLAGS = {"-s", "--select", "--exclude", "--resource-type", "--output"}
_DBT_SWITCHES = {"--quiet", "-q", "--no-partial-parse", "--fail-fast", "-x"}
_MF_FLAGS = {"--metrics", "--group-by", "--limit", "--order", "--start-time", "--end-time"}
_MF_SWITCHES = {"--quiet", "--show-all", "metrics", "dimensions", "entities"}
_META = re.compile(r"[;&|`$<>\n\\]")
_GLOB_CHARS = re.compile(r"[*?\[{]")


def allowed_patterns() -> list[str]:
    return ["Read", "Edit", "Write", "Glob", "Grep"] + [
        f"Bash({tool} {sub}:*)"
        for tool, subs in (("dbt", _DBT), ("mf", _MF))
        for sub in sorted(subs)
    ]


class WorkspaceGuard:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.denied: list[str] = []

    def _resolve(self, value: str) -> Path:
        path = Path(os.path.expanduser(value))
        return (path if path.is_absolute() else self.root / path).resolve()

    def _inside(self, value: object) -> bool:
        if not isinstance(value, str) or not value:
            return True
        resolved = self._resolve(value)
        return resolved == self.root or self.root in resolved.parents

    def __call__(self, tool: str, data: dict[str, Any]) -> str | None:
        reason = self._check(tool, data)
        if reason:
            self.denied.append(f"{tool}: {reason}")
        return reason

    def _check(self, tool: str, data: dict[str, Any]) -> str | None:
        if tool.startswith("mcp__"):
            return None
        if tool in FILE_TOOLS:
            return self._check_file(tool, data)
        if tool == "Bash":
            return self._check_bash(str(data.get("command", "")))
        return f"{tool} は使えません"

    def _check_file(self, tool: str, data: dict[str, Any]) -> str | None:
        outside = f"作業ディレクトリ（{self.root}）の外にはアクセスできません"
        for key in ("file_path", "path", "notebook_path"):
            if not self._inside(data.get(key)):
                return outside
        if tool == "Glob":
            pattern = str(data.get("pattern", ""))
            prefix = _GLOB_CHARS.split(pattern, maxsplit=1)[0]
            if ".." in Path(pattern).parts or (prefix and not self._inside(prefix or ".")):
                return outside
        if tool in WRITE_TOOLS:
            target = self._resolve(str(data.get("file_path", "")))
            if target.name in PROTECTED_FILES or target.suffix not in WRITABLE_SUFFIXES:
                return (
                    f"{target.name} は書き換えられません（書けるのは SQL / YAML / Markdown だけ）"
                )
            content = " ".join(str(data.get(k, "")) for k in ("content", "new_string")) + " ".join(
                str(e.get("new_string", "")) for e in data.get("edits", []) or []
            )
            if "env_var(" in content:
                return "env_var() は使えません"
        return None

    def _check_bash(self, command: str) -> str | None:
        if _META.search(command):
            return "シェルのメタ文字（; & | ` $ < > など）は使えません"
        try:
            argv = shlex.split(command)
        except ValueError:
            return "コマンドを解釈できません"
        if len(argv) < 2:
            return "実行できるのは dbt と mf のコマンドだけです"
        if argv[0] == "dbt" and argv[1] in _DBT:
            flags, switches = _DBT_FLAGS, _DBT_SWITCHES
        elif argv[0] == "mf" and argv[1] in _MF:
            flags, switches = _MF_FLAGS, _MF_SWITCHES
        else:
            return (
                "実行できるのは dbt（parse / build / compile / test / ls）と"
                " mf（validate-configs / query / list）だけです"
            )
        args = argv[2:]
        i = 0
        while i < len(args):
            arg = args[i]
            name, has_value, _ = arg.partition("=")
            if name in flags:
                value = _ if has_value else (args[i + 1] if i + 1 < len(args) else "")
                if "/" in value or value.startswith(("~", ".")):
                    return f"{name} にパスは指定できません"
                i += 1 if has_value else 2
            elif arg in switches or (not arg.startswith("-") and argv[0] == "mf"):
                i += 1
            else:
                return f"オプション {arg} は使えません"
        return None

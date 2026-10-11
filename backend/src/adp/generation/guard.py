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
    # dbt Charts の接続設定。独自の duckdb ソースで外部アクセスを戻せるため（ADR 0002）
    "dbt_charts.yml",
}
# チャートは生成後に adp.charts が書き出す。エージェントには書かせない（手書きの SQL を入れない）
# クレンジングの定義も同様（定義から adp.cleansing がクレンジング層を生成する。#68）
PROTECTED_DIRS = {"charts", "chart_specs", "cleansing"}
_DBT = {"parse", "build", "compile", "test", "ls", "list"}
_MF = {"validate-configs", "query", "list"}
# 値を取るオプション（値にパスを渡せないものだけ）
_DBT_FLAGS = {"-s", "--select", "--exclude", "--resource-type", "--output"}
_DBT_SWITCHES = {"--quiet", "-q", "--no-partial-parse", "--fail-fast", "-x"}
_MF_FLAGS = {"--metrics", "--group-by", "--limit", "--order", "--start-time", "--end-time"}
_MF_SWITCHES = {"--quiet", "--show-all", "metrics", "dimensions", "entities"}
_META = re.compile(r"[;&|`$<>\n\\]")
_GLOB_CHARS = re.compile(r"[*?\[{]")
# 環境変数（秘密情報）を読める Jinja の関数・属性
_SECRET_ACCESS = re.compile(
    r"\b(env_var|environ|getenv|run_query)\b|\bcall\s+statement\b|adapter\s*\.\s*execute"
)
"""秘密情報の読み出し（env_var など）と、dbt の実行中に任意の SQL を流すもの（run_query など）。"""


def allowed_patterns() -> list[str]:
    return ["Read", "Edit", "Write", "Glob", "Grep"] + [
        f"Bash({tool} {sub}:*)"
        for tool, subs in (("dbt", _DBT), ("mf", _MF))
        for sub in sorted(subs)
    ]


def _is_cleansing_output(target: Path) -> bool:
    """adp.cleansing が定義から生成したファイル（先頭行に印がある）。"""
    from adp.cleansing.render import GENERATED

    try:
        with target.open(encoding="utf-8") as f:
            return GENERATED in f.readline()
    except OSError:
        return False


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
        try:
            reason = self._check(tool, data)
        except Exception as e:  # 判定できない入力（NUL を含むパスなど）は拒否する
            reason = f"入力を判定できません: {type(e).__name__}"
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
            # macOS などのファイルシステムは大文字・小文字（Unicode の case folding。ſ → s など）を
            # 区別しない。書けるパスを ASCII に限り、小文字にして比べる
            if not all(p.isascii() for p in target.relative_to(self.root).parts):
                return "パスには英数字（ASCII）だけを使ってください"
            if (
                target.name.lower() in PROTECTED_FILES
                or target.suffix.lower() not in WRITABLE_SUFFIXES
            ):
                return (
                    f"{target.name} は書き換えられません（書けるのは SQL / YAML / Markdown だけ）"
                )
            if PROTECTED_DIRS & {p.lower() for p in target.relative_to(self.root).parts[:1]}:
                return (
                    "チャート（charts/ と chart_specs/）と"
                    "クレンジングの定義（cleansing/）は書き換えられません"
                )
            if _is_cleansing_output(target):
                return "クレンジング層（stg）は定義から生成します。直接は書き換えられません"
            if _SECRET_ACCESS.search(self._resulting_content(tool, target, data)):
                return "env_var や run_query を含む内容は書けません"
        return None

    def _resulting_content(self, tool: str, target: Path, data: dict[str, Any]) -> str:
        """書き込み後のファイル全体（Edit は既存の内容に置換を適用した結果）。

        new_string だけを見ると、複数回の編集で禁止語を組み立てられるため。
        """
        if tool == "Write":
            return str(data.get("content", ""))
        try:
            text = target.read_text(encoding="utf-8") if target.exists() else ""
        except OSError:
            text = ""
        edits = data.get("edits") or [data]
        for e in edits:
            old, new = str(e.get("old_string", "")), str(e.get("new_string", ""))
            if e.get("replace_all"):
                text = text.replace(old, new)
            else:
                text = text.replace(old, new, 1) if old else text + new
        return text

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

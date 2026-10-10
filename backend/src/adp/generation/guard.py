"""生成エージェントの操作範囲を制限する（design-doc Security、#16）。

- ファイル操作（Read / Edit / Write / Glob / Grep）は作業ツリーの中だけ
- Bash は dbt / mf の決まったサブコマンドだけ。シェルのメタ文字は使えない
- それ以外の組み込み tool は使えない
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"}
BUILTIN_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
_DBT = {"parse", "build", "compile", "test", "run", "ls", "list", "show", "deps"}
_MF = {"validate-configs", "query", "list"}
_META = re.compile(r"[;&|`$<>\n\\]")


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

    def _inside(self, value: object) -> bool:
        if not isinstance(value, str) or not value:
            return True
        path = Path(value)
        resolved = (path if path.is_absolute() else self.root / path).resolve()
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
            for key in ("file_path", "path", "notebook_path"):
                if not self._inside(data.get(key)):
                    return f"作業ディレクトリ（{self.root}）の外にはアクセスできません"
            return None
        if tool == "Bash":
            command = str(data.get("command", ""))
            if _META.search(command):
                return "シェルのメタ文字（; & | ` $ < > など）は使えません"
            try:
                argv = shlex.split(command)
            except ValueError:
                return "コマンドを解釈できません"
            if len(argv) >= 2 and (
                (argv[0] == "dbt" and argv[1] in _DBT) or (argv[0] == "mf" and argv[1] in _MF)
            ):
                return None
            return "実行できるのは dbt と mf のコマンドだけです"
        return f"{tool} は使えません"

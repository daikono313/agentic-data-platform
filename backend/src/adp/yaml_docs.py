"""ディレクトリの中の YAML ファイルの読み込み（dbt の models/ の yml、beam/ の成果物など。#110）。

壊れた YAML・dict でない文書は飛ばして `problems` に残し（ログにも出す）、ほかのファイルは
読む（1 つの壊れたファイルで呼び出し側の処理全体を落とさない）。内容が変わっていない
ファイル（パス・mtime・サイズが同じ）は読み直さない。返す dict はキャッシュと共有するので、
呼び出し側で書き換えない。

他の adp のモジュールに依存しない。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class YamlDoc:
    path: Path
    text: str
    data: dict[str, Any]

    def entries(self, key: str) -> list[dict[str, Any]]:
        """トップレベルの一覧（models・sources など）の、name のある要素。"""
        return named(self.data.get(key))


@dataclass(frozen=True)
class YamlProblem:
    path: Path
    reason: str


@dataclass(frozen=True)
class YamlDocs:
    docs: list[YamlDoc]
    """読めたファイル（パスの順）。"""
    problems: list[YamlProblem]
    """読めなかったファイル（壊れた YAML・dict でない文書など）。"""

    def entries(self, key: str) -> Iterator[tuple[YamlDoc, dict[str, Any]]]:
        """全ファイルの、トップレベルの一覧（models・sources など）の name のある要素。"""
        for doc in self.docs:
            for entry in doc.entries(key):
                yield doc, entry


def named(value: Any) -> list[dict[str, Any]]:
    """一覧のうち、name（空でない）を持つ dict の要素だけ（列・テーブルなど。それ以外は飛ばす）。"""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict) and v.get("name") not in (None, "")]


_log = logging.getLogger(__name__)
_MAX_CACHED = 4096
_cache: dict[Path, tuple[int, int, YamlDoc | YamlProblem]] = {}
_lock = threading.Lock()


def load_yaml_docs(directory: Path, pattern: str = "**/*.yml") -> YamlDocs:
    """directory の中で pattern に合うファイルを読む（既定はサブディレクトリも含む *.yml）。"""
    docs: list[YamlDoc] = []
    problems: list[YamlProblem] = []
    for path in sorted(directory.glob(pattern)):  # 順序を決まったものに
        result = _load(path)
        if isinstance(result, YamlDoc):
            docs.append(result)
        else:
            problems.append(result)
    return YamlDocs(docs, problems)


def _load(path: Path) -> YamlDoc | YamlProblem:
    try:
        st = path.stat()
    except OSError as e:
        return YamlProblem(path, f"読めません: {type(e).__name__}")
    with _lock:
        hit = _cache.get(path)
    if hit is not None and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    result = _parse(path)
    if isinstance(result, YamlProblem):  # 読み直したときだけ出す
        _log.warning("YAML を飛ばしました: %s: %s", path, result.reason)
    with _lock:
        if len(_cache) >= _MAX_CACHED:  # 消えた worktree のファイルなどが溜まり続けないように
            _cache.clear()
        _cache[path] = (st.st_mtime_ns, st.st_size, result)
    return result


def _parse(path: Path) -> YamlDoc | YamlProblem:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return YamlProblem(path, f"読めません: {type(e).__name__}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return YamlProblem(path, f"YAML として読めません（{type(e).__name__}）")
    if not isinstance(data, dict):
        return YamlProblem(path, "YAML の中身が辞書（key: value）ではありません")
    return YamlDoc(path, text, data)

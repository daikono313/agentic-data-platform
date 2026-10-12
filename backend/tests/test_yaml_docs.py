"""YAML の読み込み（壊れたファイルに強く、変わらないファイルは読み直さない。#110）。"""

import os
from pathlib import Path

import pytest

from adp import yaml_docs
from adp.yaml_docs import load_yaml_docs, named


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_broken_and_non_dict_files_are_skipped_and_reported(tmp_path: Path) -> None:
    _write(tmp_path / "a.yml", "models:\n  - name: fct_a\n")
    _write(tmp_path / "b.yml", "models: [\n  - name: :\n")  # 壊れた YAML
    _write(tmp_path / "c.yml", "- just a list\n")  # dict でない
    _write(tmp_path / "d.yml", "")  # 空
    _write(tmp_path / "sub" / "e.yml", "models:\n  - name: fct_e\n")
    (tmp_path / "g.yml").write_bytes(b"\xff\xfe\x00")  # UTF-8 でない
    docs = load_yaml_docs(tmp_path)
    assert [d.path.name for d in docs.docs] == ["a.yml", "e.yml"]
    assert [p.path.name for p in docs.problems] == ["b.yml", "c.yml", "d.yml", "g.yml"]
    assert [m["name"] for _, m in docs.entries("models")] == ["fct_a", "fct_e"]


def test_pattern_limits_to_top_level(tmp_path: Path) -> None:
    _write(tmp_path / "a.yml", "name: a\n")
    _write(tmp_path / "sub" / "b.yml", "name: b\n")
    assert [d.data["name"] for d in load_yaml_docs(tmp_path, "*.yml").docs] == ["a"]


def test_named_skips_entries_without_name() -> None:
    value = [{"name": "a"}, {"description": "x"}, {"name": None}, {"name": ""}, "str", None]
    assert named(value) == [{"name": "a"}]
    assert named(None) == [] and named({"name": "a"}) == []


def test_unchanged_files_are_not_reread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.yml"
    _write(path, "name: a\n")
    calls: list[Path] = []
    real = yaml_docs._parse

    def spy(p: Path) -> yaml_docs.YamlDoc | yaml_docs.YamlProblem:
        calls.append(p)
        return real(p)

    monkeypatch.setattr(yaml_docs, "_parse", spy)
    assert load_yaml_docs(tmp_path).docs[0].data == {"name": "a"}
    assert load_yaml_docs(tmp_path).docs[0].data == {"name": "a"}
    assert calls == [path]

    # 内容が変われば読み直す（同じサイズでも mtime で気付く）
    _write(path, "name: b\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert load_yaml_docs(tmp_path).docs[0].data == {"name": "b"}
    # 壊れても、直っても、その時の内容で返す
    _write(path, "name: [\n")
    docs = load_yaml_docs(tmp_path)
    assert docs.docs == [] and [p.path for p in docs.problems] == [path]
    _write(path, "name: ok\n")
    assert load_yaml_docs(tmp_path).docs[0].data == {"name": "ok"}
    assert len(calls) == 4

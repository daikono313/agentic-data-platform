"""eval の前後比較（#45）。プロンプトを直す前と後の結果（.adp/evals/<日時>/）を並べる。

    uv run python -m adp.evals compare .adp/evals/<前> .adp/evals/<後>

シナリオごとに、合格率と会話の質の数値（ターン数・質問の数・発言の長さ・説明なしの専門用語・
重複した質問・選択肢の位置を指す言葉・締めの一言）を平均して比べる。LLM の応答はぶれるので、
`make eval REPEAT=3` のように繰り返した結果を比べる。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from adp.evals.interview import transcript_metrics

METRICS: list[tuple[str, str, int]] = [
    # (キー, 表示名, よい向き: -1 は小さいほどよい)
    ("passed", "合格率", 1),
    ("assistant_turns", "ターン数", -1),
    ("questions", "質問の数", -1),
    ("avg_chars", "発言の平均文字数", -1),
    ("unexplained_jargon", "説明なしの専門用語", -1),
    ("repeated_questions", "重複した質問", -1),
    ("position_words", "選択肢の位置を指す言葉", -1),
    ("closing_phrases", "締めの一言", -1),
    ("repeated_text", "二重の本文", -1),
    ("effort_questions", "工数を聞いた質問", -1),
]


def check_rates(path: Path) -> dict[str, tuple[int, int]]:
    """チェックごとの (合格した回数, 行った回数)。キーは「シナリオ / チェック」（#135）。"""
    rates: dict[str, tuple[int, int]] = {}
    for file in sorted(path.glob("*.json")):
        if file.name == "meta.json":
            continue
        data = json.loads(file.read_text(encoding="utf-8"))
        for c in data.get("checks") or []:
            key = f"{data['scenario']} / {c['name']}"
            ok, n = rates.get(key, (0, 0))
            rates[key] = (ok + bool(c.get("passed")), n + 1)
    return rates


def check_table(before: Path | None, after: Path) -> list[str]:
    """チェックごとの合格率の表（前が無ければ後だけ。基準を取るときに使う）。"""
    rb = check_rates(before) if before else {}
    ra = check_rates(after)

    def cell(r: tuple[int, int] | None) -> str:
        return f"{r[0]}/{r[1]}" if r else "-"

    lines = ["| シナリオ / チェック | " + ("前 → 後 |" if before else "合格 |"), "|---|---|"]
    for key in sorted(set(rb) | set(ra)):
        b, a = rb.get(key), ra.get(key)
        if before:
            worse = b and a and a[0] / a[1] < b[0] / b[1]
            lines.append(f"| {key} | {cell(b)} → {cell(a)}{' ⚠' if worse else ''} |")
        else:
            lines.append(f"| {key} | {cell(a)} |")
    return lines


def load_run(path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, float]]]]:
    """1 回の eval（ディレクトリ）を読み、シナリオごとの数値の一覧にする。"""
    if not path.is_dir():
        raise FileNotFoundError(f"eval の結果がありません: {path}")
    meta_path = path / "meta.json"
    meta: dict[str, Any] = (
        json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    )
    runs: dict[str, list[dict[str, float]]] = defaultdict(list)
    for file in sorted(path.glob("*.json")):
        if file.name == "meta.json":
            continue
        data = json.loads(file.read_text(encoding="utf-8"))
        # 会話があれば今の定義で数え直す（検出の定義を変えても、前後を同じ物差しで比べる。#77）
        transcript = data.get("transcript")
        metrics = (
            transcript_metrics([SimpleNamespace(**({"question": None} | m)) for m in transcript])
            if transcript
            else data.get("metrics") or {}
        )
        runs[data["scenario"]].append({**metrics, "passed": float(bool(data.get("passed")))})
        meta.setdefault("prompt_version", data.get("prompt_version"))
    return meta, dict(runs)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _fmt(key: str, value: float) -> str:
    return f"{value:.0%}" if key == "passed" else f"{value:.1f}"


def compare(before: Path, after: Path) -> str:
    """Markdown の表（PR に貼る）。"""
    meta_b, runs_b = load_run(before)
    meta_a, runs_a = load_run(after)
    lines = [
        "## eval の前後比較",
        "",
        f"- 前: `{before.name}`（プロンプト {meta_b.get('prompt_version') or '-'}）",
        f"- 後: `{after.name}`（プロンプト {meta_a.get('prompt_version') or '-'}）",
        "",
    ]
    scenarios = sorted(set(runs_b) | set(runs_a))
    header = "| シナリオ | 回数（前→後） | " + " | ".join(label for _, label, _ in METRICS) + " |"
    lines += [header, "|" + "---|" * (2 + len(METRICS))]
    totals: dict[str, tuple[list[float], list[float]]] = {k: ([], []) for k, _, _ in METRICS}
    for name in scenarios:
        b, a = runs_b.get(name, []), runs_a.get(name, [])
        cells = []
        for key, _, better in METRICS:
            vb, va = _mean([r[key] for r in b]), _mean([r[key] for r in a])
            if b and a:  # 全体は前後の両方にあるシナリオだけで比べる
                totals[key][0].extend(r[key] for r in b)
                totals[key][1].extend(r[key] for r in a)
            mark = "" if not b or not a or va == vb else ("✅" if (va - vb) * better > 0 else "⚠")
            cells.append(
                f"{_fmt(key, vb) if b else '-'} → {_fmt(key, va) if a else '-'} {mark}".strip()
            )
        lines.append(f"| {name} | {len(b)}→{len(a)} | " + " | ".join(cells) + " |")
    cells = []
    for key, _, better in METRICS:
        vb, va = _mean(totals[key][0]), _mean(totals[key][1])
        mark = "" if va == vb else ("✅" if (va - vb) * better > 0 else "⚠")
        cells.append(f"**{_fmt(key, vb)} → {_fmt(key, va)}** {mark}".strip())
    lines.append("| **全体（前後の両方にあるシナリオ）** | | " + " | ".join(cells) + " |")
    lines += ["", "### チェックごとの合格率", "", *check_table(before, after)]
    lines += [
        "",
        "✅ はよくなった、⚠ は悪くなった数値。前と後は同じ eval のコードで実行すること"
        "（チェックや数値の定義が変わると比べられない）。",
    ]
    return "\n".join(lines) + "\n"

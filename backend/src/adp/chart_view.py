"""描画画面: 指標を選び、切り口・粒度・期間を変えてチャートを見る（#43、ADR 0003）。

画面から受け取るのは指標名・切り口名・粒度・期間だけで、どれも Semantic Layer の一覧
（`adp.datasets`）と照らし合わせて検査する。SQL はプロセス内の MetricFlow が作り、
dbt Charts がプロセス内で SVG に描く。ボードの検査は生成時と同じ許可リスト（`adp.charts`）を通す。
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from adp.charts import BoardRenderer, ChartError, build_board
from adp.datasets import DatasetIndex, MetricInfo
from adp.generation.chart_specs import BAR_LIMIT, safe_label

Grain = Literal["day", "week", "month", "quarter", "year"]


class ChartRequest(BaseModel):
    metric: str
    dimension: str | None = None
    """カテゴリの切り口（例: store__channel）。"""
    grain: Grain | None = "month"
    """時間の粒度。None なら時間で分けない（棒 / KPI）。"""
    start: date | None = None
    end: date | None = None


class ChartResult(BaseModel):
    svg: str
    kind: str
    """line / bar / kpi"""
    title: str
    sql: str


class ChartView:
    """1 つの warehouse（main、または生成ジョブの worktree）の描画。"""

    def __init__(self, warehouse_root: Path) -> None:
        self.index = DatasetIndex(warehouse_root, None)
        self.renderer = BoardRenderer(self.index.warehouse)

    def metrics(self) -> tuple[list[MetricInfo], list[str]]:
        errors: list[str] = []
        beams = self.index.beams(errors)
        return self.index.metrics(beams, errors), errors

    def spec(self, req: ChartRequest) -> tuple[dict[str, Any], str, str]:
        metrics, _ = self.metrics()
        metric = next((m for m in metrics if m.name == req.metric), None)
        if metric is None:
            raise ChartError(f"指標 {req.metric} がありません")
        dim = None
        if req.dimension:
            dim = next(
                (
                    d
                    for d in metric.dimensions
                    if d.name == req.dimension and d.kind == "categorical"
                ),
                None,
            )
            if dim is None:
                raise ChartError(f"{metric.label} は {req.dimension} で切れません")
        if req.start and req.end and req.start > req.end:
            raise ChartError("期間の開始が終了より後になっています")
        grain = req.grain
        if grain is None and metric.type == "cumulative":
            grain = "month"  # 累計は時間の切り口が必要
        label = safe_label(metric.label) or metric.name
        title = label + (f"（{safe_label(dim.label)}別）" if dim else "")
        query: dict[str, Any] = {"metrics": [metric.name]}
        chart: dict[str, Any] = {"query": "q", "title": title}
        if grain:
            x = f"metric_time__{grain}"
            query |= {"group_by": [x, *([dim.name] if dim else [])], "order": [x]}
            chart |= {"type": "line", "x": x, "y": metric.name, "y_label": label}
            if dim:
                chart["color"] = dim.name
            kind = "line"
        elif dim:
            query |= {"group_by": [dim.name], "order": [f"-{metric.name}"], "limit": BAR_LIMIT}
            chart |= {"type": "bar", "x": dim.name, "y": metric.name, "y_label": label}
            chart["sort"] = {"by": metric.name, "order": "desc"}
            kind = "bar"
        else:
            chart = {"type": "kpi", "query": "q", "label": title, "value": metric.name}
            kind = "kpi"
        if req.start:
            query["start_time"] = req.start.isoformat()
        if req.end:
            query["end_time"] = req.end.isoformat()
        # タイトルはチャートにだけ付ける（画面の見出しと重ねない）
        spec = {"metric_queries": {"q": query}, "charts": {"c": chart}, "rows": ["c"]}
        return spec, kind, title

    def render(self, req: ChartRequest) -> ChartResult:
        spec, kind, title = self.spec(req)
        board = build_board("view", spec, self.index.compiler.sql)
        return ChartResult(
            svg=self.renderer.svg(board),
            kind=kind,
            title=title,
            sql=str(board["queries"]["q"]["sql"]),
        )


class ChartViews:
    """main の warehouse と、生成ジョブの worktree ごとの描画をまとめる。"""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.worktrees = (repo_root / ".adp" / "worktrees").resolve()
        self._views: dict[Path, ChartView] = {}
        self._lock = threading.Lock()

    def _get(self, root: Path) -> ChartView:
        with self._lock:
            if root not in self._views:
                self._views[root] = ChartView(root)
            return self._views[root]

    def main(self) -> ChartView:
        return self._get(self.repo_root)

    def for_branch(self, branch: str) -> ChartView:
        """生成ジョブの作業ブランチの worktree（PR の内容をマージ前に見る）。"""
        root = (self.worktrees / branch.replace("/", "-")).resolve()
        if root.parent != self.worktrees or not (root / "warehouse").is_dir():
            raise ChartError("この生成ジョブの作業ディレクトリがありません（片付け済みの可能性）")
        return self._get(root)

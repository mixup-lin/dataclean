#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""场景化展示模板引擎（数据处理场景 ↔ 展示方式）。

设计目标
--------
把「数据处理场景」与「展示方式」解耦，形成一套可配置、可扩展的展示层：

* :data:`SCENARIO_CATALOG` 系统梳理各典型场景（清洗 / 统计 / 分组 / 趋势 /
  异常 / 占比 / 排名 / 分布 / 相关 / 透视 / 合并 / 明细）的推荐展示块与图表类型；
* :func:`build_plan` 依据 **数据类型、字段特征、结果规模、展示目标** 产出展示计划，
  同一份结果在不同目标下可切换模板（演示 / 汇报 / 使用说明）；
* :data:`BLOCK_RENDERERS` 为可注册的块渲染器注册表，新增展示形态只需注册一个函数；
* :func:`render_document` 产出结构清晰、样式统一、说明完整的自包含 HTML。

兼容契约
--------
既有 ``report_task.render_sections`` / ``render_task_report`` 的函数签名与关键标记
（``chart-section``、``时间趋势`` 等）保持不变；本模块作为其背后的模板引擎被复用，
也可独立调用以生成演示 / 汇报 / 使用说明页面。
"""
from __future__ import annotations

import html as _html
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from charts_svg import render_chart
from formatting import esc, fmt_num

LOGGER = logging.getLogger("etl")

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"
STATIC = ROOT / "static"

# 展示目标：决定块的取舍与说明详略
GOAL_LABELS = {
    "demo": "演示",
    "report": "汇报",
    "guide": "使用说明",
}

# 展示块中文名（用于说明与目录页，避免把目标标签错用于块名）
BLOCK_LABELS = {
    "kpi": "指标卡片",
    "chart": "图表",
    "table": "数据表格",
    "summary": "摘要说明",
    "note": "口径说明",
    "quality": "质量附录",
    "chain": "算子链路",
    "join": "关联质量",
}

# 结果规模分档（行数 × 列数）
SCALE_SMALL = "small"
SCALE_MEDIUM = "medium"
SCALE_LARGE = "large"

MAX_TABLE_ROWS = {"small": 50, "medium": 50, "large": 30}


@dataclass(frozen=True)
class ScenarioSpec:
    """一个数据处理场景的展示规格。"""

    key: str
    label: str
    description: str
    blocks: Tuple[str, ...]
    chart_kinds: Tuple[str, ...]
    hint: str


# ---------------------------------------------------------------------------
# 场景 → 展示方式 系统梳理（单一事实源，便于审阅与扩展）
# ---------------------------------------------------------------------------
SCENARIO_CATALOG: Dict[str, ScenarioSpec] = {
    "cleaning": ScenarioSpec(
        key="cleaning",
        label="数据清洗",
        description="去重、填充、筛选、异常处置等行 / 列级整理，关注清洗前后差异与处置明细。",
        blocks=("kpi", "table", "quality", "note"),
        chart_kinds=("hbar", "column"),
        hint="卡片展示清洗规模，表格列出处置明细，附录给出列级质量基线。",
    ),
    "statistics": ScenarioSpec(
        key="statistics",
        label="统计分析",
        description="描述性统计与整体指标，关注集中趋势、离散程度与数值分布。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("hbar", "column", "donut"),
        hint="卡片给出核心指标，图表展示数值分布，表格保留统计摘要。",
    ),
    "group_summary": ScenarioSpec(
        key="group_summary",
        label="分组汇总",
        description="按维度聚合后的分组对比，关注组间差异与头部组贡献。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("hbar", "column", "donut"),
        hint="卡片高亮头部分组，条形图对比组间数值，表格承载完整分组结果。",
    ),
    "trend": ScenarioSpec(
        key="trend",
        label="趋势对比",
        description="按时间桶聚合的周期趋势，关注峰值、谷值与末期环比。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("line", "column"),
        hint="折线图呈现时间走势，卡片标注峰值与环比，表格逐期列出数值。",
    ),
    "anomaly": ScenarioSpec(
        key="anomaly",
        label="异常识别",
        description="基于统计边界或规则标记的异常点，关注异常范围、数量与处置动作。",
        blocks=("kpi", "table", "note", "quality"),
        chart_kinds=("column", "hbar"),
        hint="卡片给出异常规模，表格列出检测边界与动作，必要时补一张分布图。",
    ),
    "proportion": ScenarioSpec(
        key="proportion",
        label="占比构成",
        description="分子 / 分母比值或构成拆解，关注比率、分子分母与结构占比。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("donut", "pareto", "hbar"),
        hint="环形图展示构成，卡片给出比率与分子分母，表格保留占比明细。",
    ),
    "ranking": ScenarioSpec(
        key="ranking",
        label="排名分析",
        description="按指标降序排列的头部条目，关注名次、头部领先度与集中度。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("hbar", "pareto", "column"),
        hint="横向条形图呈现名次，卡片标注冠军与前 3 集中度，表格为排名明细。",
    ),
    "distribution": ScenarioSpec(
        key="distribution",
        label="分布分析",
        description="分箱或取值分布，关注区间结构、长尾与集中区间。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("column", "hbar"),
        hint="柱状图展示区间分布，卡片高亮主区间，表格列出各区间计数。",
    ),
    "correlation": ScenarioSpec(
        key="correlation",
        label="相关分析",
        description="数值字段间的相关关系，关注相关强度、方向与散点形态。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("scatter", "heatmap"),
        hint="散点图呈现两变量关系，热力图给出相关矩阵，表格保留系数明细。",
    ),
    "pivot": ScenarioSpec(
        key="pivot",
        label="透视分析",
        description="二维交叉汇总，关注行列交叉处的数值密度与结构热点。",
        blocks=("kpi", "chart", "table", "note"),
        chart_kinds=("heatmap",),
        hint="热力图呈现交叉热点，表格保留透视原表，卡片给出交叉规模。",
    ),
    "join": ScenarioSpec(
        key="join",
        label="多表合并",
        description="主表与附加表关联，关注匹配率、未匹配明细与关联后规模。",
        blocks=("kpi", "join", "table", "note"),
        chart_kinds=("hbar",),
        hint="关联质量卡展示匹配率，卡片给出关联规模，表格保留合并结果。",
    ),
    "detail": ScenarioSpec(
        key="detail",
        label="明细查看",
        description="逐行明细与清洗前后对照，关注具体记录而非聚合趋势。",
        blocks=("kpi", "table", "note"),
        chart_kinds=(),
        hint="以表格为主体逐行查看，卡片给出结果规模，不渲染聚合图表。",
    ),
}

# task_type（画像）→ 展示场景
_TASK_TO_SCENARIO = {
    "proportion": "proportion",
    "ranking": "ranking",
    "trend": "trend",
    "distribution": "distribution",
    "correlation": "correlation",
    "pivot": "pivot",
    "join": "join",
    "summary": "group_summary",
    "detail": "detail",
    "rate": "proportion",
}


# ---------------------------------------------------------------------------
# 展示计划数据结构
# ---------------------------------------------------------------------------
@dataclass
class BlockSpec:
    """一个展示块（卡片 / 图表 / 表格 / 摘要等）的声明。"""

    kind: str
    title: str
    hint: str = ""
    source: str = ""
    chart_kinds: Tuple[str, ...] = ()
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "hint": self.hint,
            "source": self.source,
            "chart_kinds": list(self.chart_kinds),
            "config": dict(self.config),
        }


@dataclass
class DisplayPlan:
    """展示计划：场景 + 目标 + 有序展示块 + 选择理由。"""

    scenario: str
    goal: str
    blocks: List[BlockSpec]
    scale: str
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "goal": self.goal,
            "scale": self.scale,
            "blocks": [b.to_dict() for b in self.blocks],
            "reasons": list(self.reasons),
        }


@dataclass
class DisplayContext:
    """渲染上下文：画像、结果数据与执行产物。"""

    profile: Any
    rule: Dict[str, Any]
    frame: pd.DataFrame
    clean_report: Dict[str, Any] = field(default_factory=dict)
    step_logs: List[Dict[str, Any]] = field(default_factory=list)
    join_stats: List[Dict[str, Any]] = field(default_factory=list)
    base_df: Optional[pd.DataFrame] = None

    @property
    def kpis(self) -> List[Dict[str, Any]]:
        return list(getattr(self.profile, "kpis", []) or [])

    @property
    def charts(self) -> List[Dict[str, Any]]:
        return list(getattr(self.profile, "charts", []) or [])

    @property
    def tables(self) -> List[Dict[str, Any]]:
        return list(getattr(self.profile, "tables", []) or [])

    @property
    def conclusion(self) -> str:
        return str(getattr(self.profile, "conclusion", "") or "")

    @property
    def note(self) -> str:
        return str(getattr(self.profile, "note", "") or "")

    def main_frame(self) -> pd.DataFrame:
        if self.tables:
            candidate = self.tables[0].get("frame")
            if isinstance(candidate, pd.DataFrame):
                return candidate
        return self.frame if isinstance(self.frame, pd.DataFrame) else pd.DataFrame()


# ---------------------------------------------------------------------------
# 场景识别与规模判定
# ---------------------------------------------------------------------------
def _data_scale(frame: pd.DataFrame) -> str:
    rows = len(frame)
    cols = len(frame.columns) if isinstance(frame, pd.DataFrame) else 0
    if rows <= 50 and cols <= 12:
        return SCALE_SMALL
    if rows <= 500 and cols <= 24:
        return SCALE_MEDIUM
    return SCALE_LARGE


def _field_features(frame: pd.DataFrame) -> Dict[str, Any]:
    """从结果表推导字段特征，供块选择使用（不依赖用户原话）。"""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {"numeric": [], "categorical": [], "datetime": [], "high_card": []}
    numeric, categorical, dt, high_card = [], [], [], []
    n = len(frame)
    for col in frame.columns:
        s = frame[col]
        if pd.api.types.is_numeric_dtype(s):
            numeric.append(str(col))
        elif pd.api.types.is_datetime64_any_dtype(s):
            dt.append(str(col))
        else:
            categorical.append(str(col))
        try:
            if s.nunique(dropna=True) > min(50, max(2, int(n * 0.9))):
                high_card.append(str(col))
        except (TypeError, ValueError):
            continue
    return {
        "numeric": numeric,
        "categorical": categorical,
        "datetime": dt,
        "high_card": high_card,
    }


def detect_scenario(ctx: DisplayContext) -> Tuple[str, List[str]]:
    """依据画像、算子与数据特征识别场景，返回 (key, 理由列表)。"""
    reasons: List[str] = []
    task_type = str(getattr(ctx.profile, "task_type", "") or "")
    # detail 是弱信号（画像兜底），继续用算子 / 清洗产物识别更贴切的场景
    if task_type in _TASK_TO_SCENARIO and task_type != "detail":
        key = _TASK_TO_SCENARIO[task_type]
        reasons.append(f"任务画像判定为 {task_type}，映射到「{SCENARIO_CATALOG[key].label}」场景")
        return key, reasons

    ops = [str(s.get("op", "")) for s in (ctx.rule.get("steps") or []) if isinstance(s, dict)]
    op_set = set(ops)
    feats = _field_features(ctx.frame)
    outliers = int(ctx.clean_report.get("outliers", {}).get("count", 0) or 0)

    if outliers > 0 and "aggregate" not in op_set:
        reasons.append(f"检测到 {outliers} 个异常值且无聚合步骤")
        return "anomaly", reasons
    if {"dedupe", "fill", "dropna", "outlier"} & op_set and "aggregate" not in op_set:
        reasons.append("算子以清洗类为主，无聚合步骤")
        return "cleaning", reasons
    if "pivot" in op_set:
        return "pivot", ["算子链包含 pivot"]
    if "join" in op_set or "union" in op_set:
        return "join", ["算子链包含关联操作"]
    if "rate" in op_set or "pct" in op_set:
        return "proportion", ["算子链包含比值 / 占比操作"]
    if "sort" in op_set or "rank" in op_set:
        return "ranking", ["算子链包含排序 / 排名操作"]
    if feats["datetime"] and "aggregate" in op_set:
        return "trend", ["存在时间字段且包含聚合步骤"]
    if "aggregate" in op_set:
        return "group_summary", ["算子链包含聚合步骤"]
    reasons.append("未命中特定场景，按明细查看展示")
    return "detail", reasons


def build_plan(
    ctx: DisplayContext,
    *,
    goal: str = "report",
    template: Optional[str] = None,
    blocks: Optional[Sequence[str]] = None,
) -> DisplayPlan:
    """根据场景、数据类型、字段特征、结果规模与展示目标构建展示计划。

    ``template`` / ``blocks`` 为显式配置入口：前者覆盖场景，后者覆盖块序列；
    未配置时按场景目录自动选择，保证「灵活选择或配置」。
    """
    goal = goal if goal in GOAL_LABELS else "report"
    frame = ctx.main_frame()
    # 规模取主结果与原始帧的较大者，避免画像截断 200 行导致规模误判
    if isinstance(ctx.frame, pd.DataFrame) and len(ctx.frame) > len(frame):
        frame = ctx.frame
    scale = _data_scale(frame)
    scenario, reasons = detect_scenario(ctx)

    # 显式场景模板覆盖
    if template:
        if template in SCENARIO_CATALOG:
            if template != scenario:
                reasons.append(f"显式指定展示模板「{SCENARIO_CATALOG[template].label}」")
            scenario = template
        else:
            reasons.append(f"未知展示模板 {template!r}，回退自动场景")

    spec = SCENARIO_CATALOG[scenario]
    kinds = list(blocks) if blocks else list(spec.blocks)
    # 只保留已声明或注册表支持的块类型
    block_specs: List[BlockSpec] = []
    for kind in kinds:
        block = _make_block(kind, spec, ctx, goal, scale)
        if block is not None:
            block_specs.append(block)

    # 目标驱动：演示突出图表、汇报补充结论、说明补充链路与选择理由
    if goal == "guide":
        if not any(b.kind == "chain" for b in block_specs):
            block_specs.append(_make_block("chain", spec, ctx, goal, scale))
        reasons.append("使用说明目标：追加算子链路，展开选择理由")
    if goal == "demo" and scale == SCALE_LARGE:
        reasons.append("演示目标 + 大规模结果：表格行数收紧，图表仅用聚合小数据")

    if scale == SCALE_LARGE:
        reasons.append(f"结果规模 {len(frame)} 行 × {len(frame.columns)} 列，表格行数上限收紧")
    elif scale == SCALE_SMALL:
        reasons.append(f"结果规模较小（{len(frame)} 行），保留完整明细")

    # 空载荷块剪枝：无图表则去掉 chart 块，无 join 统计则去掉 join 块
    pruned: List[BlockSpec] = []
    for block in block_specs:
        if block.kind == "chart":
            allowed = set(block.chart_kinds or ())
            surviving = [
                c for c in ctx.charts
                if isinstance(c, dict) and (not allowed or str(c.get("kind")) in allowed)
            ]
            if not surviving:
                continue
        if block.kind == "join" and not ctx.join_stats:
            continue
        if block.kind == "quality" and not _has_quality(ctx):
            continue
        pruned.append(block)
    if not pruned:
        pruned = [_make_block("table", spec, ctx, goal, scale)]  # 兜底至少一张表

    return DisplayPlan(
        scenario=scenario, goal=goal, blocks=pruned, scale=scale, reasons=reasons
    )


def _has_quality(ctx: DisplayContext) -> bool:
    if ctx.clean_report:
        return True
    frame = ctx.main_frame()
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return False
    return bool(int(frame.isna().sum().sum()) or int(frame.duplicated().sum()))


def _make_block(
    kind: str, spec: ScenarioSpec, ctx: DisplayContext, goal: str, scale: str
) -> Optional[BlockSpec]:
    """依据块类型与上下文生成块声明（标题 / 提示 / 数据源 / 配置）。"""
    if kind == "kpi":
        return BlockSpec("kpi", "指标概览", "本次任务的关键指标", "kpis",
                         config={"limit": 6})
    if kind == "chart":
        # 优先沿用画像面板的标题（保持既有标记，如「时间趋势」）
        title = ""
        for panel in (getattr(ctx.profile, "panels", []) or []):
            if isinstance(panel, dict) and panel.get("kind") == "chart":
                title = str(panel.get("title", ""))
                break
        title = title or spec.label + "图表"
        kinds = tuple(panel_chart_kinds(ctx)) or spec.chart_kinds
        return BlockSpec("chart", title, spec.hint, "charts", kinds,
                         config={"limit": 3})
    if kind == "join":
        return BlockSpec("join", "关联质量", "匹配率与未匹配行数", "join_stats",
                         config={"limit": 5})
    if kind == "table":
        title = "结果明细"
        for panel in (getattr(ctx.profile, "panels", []) or []):
            if isinstance(panel, dict) and panel.get("kind") == "table":
                title = str(panel.get("title", "")) or title
                break
        return BlockSpec("table", title, "分页友好：表格可横向滚动", "tables",
                         config={"max_rows": MAX_TABLE_ROWS[scale], "max_cols": 24})
    if kind == "summary":
        return BlockSpec("summary", "任务结论", "由本次任务结果自动归纳", "conclusion")
    if kind == "note":
        return BlockSpec("note", "口径说明", "计算口径与使用提示", "note")
    if kind == "quality":
        return BlockSpec("quality", "数据质量附录", "缺失、重复、异常与清洗策略", "quality")
    if kind == "chain":
        return BlockSpec("chain", "算子链路", "本次任务实际执行的算子顺序", "chain")
    return None


def panel_chart_kinds(ctx: DisplayContext) -> Tuple[str, ...]:
    for panel in (getattr(ctx.profile, "panels", []) or []):
        if isinstance(panel, dict) and panel.get("kind") == "chart":
            return tuple(str(k) for k in (panel.get("chart_kinds") or []))
    return ()


# ---------------------------------------------------------------------------
# 块渲染器注册表（可扩展）
# ---------------------------------------------------------------------------
BLOCK_RENDERERS: Dict[str, Callable[[BlockSpec, DisplayContext], str]] = {}


def register_block(kind: str):
    """注册自定义块渲染器；``kind`` 与 :data:`SCENARIO_CATALOG` 中的块名对应。"""
    def deco(fn: Callable[[BlockSpec, DisplayContext], str]):
        BLOCK_RENDERERS[kind] = fn
        return fn
    return deco


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)):
        return esc(fmt_num(value))
    return esc(str(value))


def _table_html(frame: pd.DataFrame, max_rows: int, max_cols: int) -> str:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return '<div class="alert info">本次任务没有可展示的表格数据。</div>'
    cols = list(frame.columns)[:max_cols]
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    rows: List[str] = []
    for values in frame.head(max_rows).itertuples(index=False, name=None):
        cells = "".join(f"<td>{_cell(v)}</td>" for v in values[:max_cols])
        rows.append(f"<tr>{cells}</tr>")
    truncated = ""
    if len(frame) > max_rows:
        truncated = (f'<p class="hint">共 {fmt_num(len(frame))} 行，'
                     f"此处展示前 {max_rows} 行。</p>")
    return (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        + head + "</tr></thead><tbody>" + "".join(rows)
        + "</tbody></table></div>" + truncated
    )


@register_block("kpi")
def _render_kpi(block: BlockSpec, ctx: DisplayContext) -> str:
    cards: List[str] = []
    for item in ctx.kpis[: block.config.get("limit", 6)]:
        unit = str(item.get("unit") or "")
        value = item.get("value")
        text = value if isinstance(value, str) else fmt_num(value)
        if unit and text not in ("—", ""):
            text = f'{text}<span class="unit">{esc(unit)}</span>'
        cards.append(
            f'<div class="kpi"><div class="kpi-v">{text}</div>'
            f'<div class="kpi-l">{esc(item.get("label", ""))}</div>'
            f'<div class="kpi-h">{esc(item.get("hint", ""))}</div></div>'
        )
    return f'<div class="kpi-band">{"".join(cards)}</div>' if cards else ""


@register_block("chart")
def _render_chart(block: BlockSpec, ctx: DisplayContext) -> str:
    allowed = set(block.chart_kinds or ())
    charts = [
        c for c in ctx.charts
        if isinstance(c, dict) and (not allowed or str(c.get("kind")) in allowed)
    ][: block.config.get("limit", 3)]
    cells: List[str] = []
    for chart in charts:
        try:
            svg = render_chart(chart)
        except Exception as exc:  # noqa: BLE001 - 单图失败不影响整体
            LOGGER.warning("图表渲染失败：%s: %s", type(exc).__name__, exc)
            continue
        if not svg:
            continue
        cells.append(
            f'<div class="chart-card"><h3>{esc(chart.get("title", "图表"))}</h3>'
            f'<div class="chart-body">{svg}</div></div>'
        )
    return f'<div class="chart-grid">{"".join(cells)}</div>' if cells else ""


@register_block("table")
def _render_table(block: BlockSpec, ctx: DisplayContext) -> str:
    frame = ctx.main_frame()
    return _table_html(frame, block.config.get("max_rows", 50),
                       block.config.get("max_cols", 24))


@register_block("join")
def _render_join(block: BlockSpec, ctx: DisplayContext) -> str:
    items: List[str] = []
    for stat in ctx.join_stats[: block.config.get("limit", 5)]:
        alias = esc(stat.get("alias", "附加表"))
        how = esc(stat.get("how", "left"))
        matched = fmt_num(stat.get("matched", 0))
        rate = stat.get("match_rate")
        rate_text = f"{float(rate):.1f}" if isinstance(rate, (int, float)) else "—"
        unmatched = fmt_num(stat.get("unmatched_left", 0))
        items.append(
            f'<div class="contract-box"><div class="t">{alias} · {how} join</div>'
            f'<div class="d">匹配 <b>{matched}</b> 行 ｜ 匹配率 <b>{rate_text}%</b> '
            f"｜ 主表未匹配 <b>{unmatched}</b> 行</div></div>"
        )
    return f'<div class="join-grid">{"".join(items)}</div>' if items else ""


@register_block("summary")
def _render_summary(block: BlockSpec, ctx: DisplayContext) -> str:
    return f'<p class="conclusion">{esc(ctx.conclusion or "—")}</p>'


@register_block("note")
def _render_note(block: BlockSpec, ctx: DisplayContext) -> str:
    text = ctx.note
    if not text:
        return ""
    return f'<p class="note-text">{esc(text)}</p>'


@register_block("quality")
def _render_quality(block: BlockSpec, ctx: DisplayContext) -> str:
    frame = ctx.main_frame()
    rows, cols = len(frame), len(frame.columns)
    cells = max(rows * max(cols, 1), 1)
    missing = int(frame.isna().sum().sum()) if isinstance(frame, pd.DataFrame) else 0
    duplicates = int(ctx.clean_report.get("duplicates_removed", 0))
    outliers = int(ctx.clean_report.get("outliers", {}).get("count", 0) or 0)
    items = [
        ("结果规模", f"{fmt_num(rows)} 行 × {cols} 列"),
        ("缺失单元格", f"{fmt_num(missing)}（{missing / cells * 100:.2f}%）"),
        ("去重删除", f"{fmt_num(duplicates)} 行"),
        ("异常值", f"{fmt_num(outliers)} 个"),
        ("填充策略", str(ctx.clean_report.get("fill_missing", "—"))),
    ]
    summary = "".join(
        f'<div class="kpi"><div class="kpi-v">{esc(value)}</div>'
        f'<div class="kpi-l">{esc(label)}</div></div>'
        for label, value in items
    )
    logs = ""
    if ctx.step_logs:
        log_rows = "".join(
            f'<tr><td>{esc(fmt_num(rec.get("index", 0)))}</td>'
            f'<td>{esc(rec.get("op", ""))}</td>'
            f'<td>{esc(rec.get("status", ""))}</td>'
            f'<td>{esc(fmt_num(rec.get("rows_before", 0)))} → '
            f'{esc(fmt_num(rec.get("rows_after", 0)))}</td>'
            f'<td>{esc(rec.get("note", ""))}</td></tr>'
            for rec in ctx.step_logs if isinstance(rec, dict)
        )
        logs = (
            '<div class="table-scroll" style="margin-top:14px">'
            '<table class="data-table"><thead><tr><th>#</th><th>算子</th><th>状态</th>'
            f"<th>行数变化</th><th>说明</th></tr></thead><tbody>{log_rows}</tbody></table></div>"
        )
    return f'<div class="kpi-band appendix-kpis">{summary}</div>{logs}'


@register_block("chain")
def _render_chain(block: BlockSpec, ctx: DisplayContext) -> str:
    steps = [s for s in (ctx.rule.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return ""
    status: Dict[int, str] = {}
    for rec in ctx.step_logs or []:
        if isinstance(rec, dict):
            status[int(rec.get("index", -1))] = str(rec.get("status", ""))
    items: List[str] = []
    for index, step in enumerate(steps):
        op = str(step.get("op", ""))
        state = status.get(index, "")
        mark = f' <span class="unit">{esc(state)}</span>' if state else ""
        items.append(f'<li><code>{esc(op)}</code>{mark}</li>')
    return f'<ol class="chain-list">{"".join(items)}</ol>'


def render_section(block: BlockSpec, ctx: DisplayContext, scenario: str = "") -> str:
    renderer = BLOCK_RENDERERS.get(block.kind)
    if renderer is None:
        return ""
    body = renderer(block, ctx)
    if not body:
        return ""
    hint = f'<p class="hint">{esc(block.hint)}</p>' if block.hint else ""
    cls = " chart-section" if block.kind == "chart" else ""
    return (
        f'<section class="card report-section{cls}" data-block="{esc(block.kind)}" '
        f'data-scenario="{esc(scenario)}">'
        f"<h2>{esc(block.title)}</h2>{hint}{body}</section>"
    )


def render_sections(plan: DisplayPlan, ctx: DisplayContext) -> str:
    """按展示计划渲染章节（仅渲染计划内块，结论由调用方统一编排）。"""
    pieces = [render_section(b, ctx, plan.scenario) for b in plan.blocks]
    return "\n".join(p for p in pieces if p)


def _inline_style() -> str:
    """内联站点样式（去掉 CDN @import，保证离线打开也完整可用）。"""
    try:
        text = (STATIC / "style.css").read_text(encoding="utf-8")
    except OSError:
        return "body{font-family:sans-serif;margin:0;padding:24px}"
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("@import")
    )


def render_document(
    plan: DisplayPlan,
    ctx: DisplayContext,
    *,
    title: str,
    kicker: str,
    subtitle: str,
    badge: str,
    meta: str,
    footer: str,
) -> str:
    """渲染完整自包含 HTML 文档（结构：头图 → 元信息 → 章节 → 页脚）。"""
    template = (PAGES / "report.html").read_text(encoding="utf-8")
    values = {
        "TITLE": esc(title),
        "KICKER": esc(kicker),
        "SUBTITLE": esc(subtitle),
        "BADGE": esc(badge),
        "META": meta,
        "SECTIONS": render_sections(plan, ctx),
        "STYLE": _inline_style(),
        "FOOTER": esc(footer),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def plan_explanation(plan: DisplayPlan) -> str:
    """生成展示计划的文字说明，用于「使用说明」目标与目录页。"""
    spec = SCENARIO_CATALOG[plan.scenario]
    block_names = " → ".join(
        BLOCK_LABELS.get(b.kind, b.kind) for b in plan.blocks
    )
    lines = [
        f"场景：{spec.label}（{spec.description}）",
        f"展示目标：{GOAL_LABELS.get(plan.goal, plan.goal)} ｜ 结果规模：{plan.scale}",
        f"展示块：{block_names}",
        f"选择理由：{'；'.join(plan.reasons) if plan.reasons else '按场景目录默认编排'}",
    ]
    return "\n".join(lines)

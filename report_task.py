#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务导向 HTML 报告：按任务画像渲染 KPI、SVG 图表、结果表与数据质量附录。

与旧 ``generate_report`` 并存：旧报告服务于原 CLI 的质量视角，本模块服务于
规则链的「任务视角」。报告为自包含单文件（内联样式与 SVG），可离线打开与打印。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from charts_svg import render_chart
from etl_pipeline import _esc, _fmt_num
from task_profile import TASK_LABELS, TaskProfile

LOGGER = logging.getLogger("etl")

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"
STATIC = ROOT / "static"

MAX_REPORT_ROWS = 50
MAX_AUX_TABLES = 3


def _style() -> str:
    """内联站点样式（去掉 CDN @import，保证离线打开也完整可用）。"""
    try:
        text = (STATIC / "style.css").read_text(encoding="utf-8")
    except OSError:
        return "body{font-family:sans-serif;margin:0;padding:24px}"
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("@import")
    )


def _render_template(**values: Any) -> str:
    text = (PAGES / "report.html").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return _fmt_num(value)


def _kpis_html(kpis: List[Dict[str, Any]]) -> str:
    if not kpis:
        return ""
    cards = []
    for item in kpis:
        unit = str(item.get("unit") or "")
        value = item.get("value")
        text = value if isinstance(value, str) else _fmt(value)
        if unit and text not in ("—", ""):
            text = f"{text}<span class=\"unit\">{_esc(unit)}</span>"
        cards.append(
            f'<div class="kpi"><div class="kpi-v">{text}</div>'
            f'<div class="kpi-l">{_esc(item.get("label", ""))}</div>'
            f'<div class="kpi-h">{_esc(item.get("hint", ""))}</div></div>'
        )
    return "".join(cards)


def _charts_html(charts: List[Dict[str, Any]]) -> str:
    blocks = []
    for chart in charts[:3]:
        svg = render_chart(chart)
        if not svg:
            continue
        blocks.append(
            f'<div class="chart-card"><h3>{_esc(chart.get("title", "图表"))}</h3>'
            f'<div class="chart-body">{svg}</div></div>'
        )
    return "".join(blocks)


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
        return _esc(_fmt_num(value))
    return _esc(str(value))


def _table_html(frame: Optional[pd.DataFrame], limit: int = MAX_REPORT_ROWS) -> str:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return '<div class="alert info">本次任务没有可展示的表格数据。</div>'
    head = "".join(f"<th>{_esc(c)}</th>" for c in frame.columns[:24])
    rows = []
    for values in frame.head(limit).itertuples(index=False, name=None):
        cells = "".join(f"<td>{_cell(v)}</td>" for v in values[:24])
        rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        + head + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _aux_html(tables: List[Dict[str, Any]]) -> str:
    blocks = []
    for entry in tables[:MAX_AUX_TABLES]:
        frame = entry.get("frame")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        blocks.append(
            f'<div class="card"><h2>{_esc(entry.get("name", "附表"))}</h2>'
            f'<p class="hint">{_esc(entry.get("note", "附表"))}，最多展示 '
            f"{MAX_REPORT_ROWS} 行。</p>{_table_html(frame)}</div>"
        )
    return "".join(blocks)


def _appendix(
    frame: pd.DataFrame,
    clean_report: Dict[str, Any],
    step_logs: Optional[List[Dict[str, Any]]],
) -> str:
    rows, cols = len(frame), len(frame.columns)
    cells = max(rows * max(cols, 1), 1)
    missing = int(frame.isna().sum().sum())
    duplicates = int(clean_report.get("duplicates_removed", 0))
    outliers = int(clean_report.get("outliers", {}).get("count", 0) or 0)
    items = [
        ("结果规模", f"{_fmt_num(rows)} 行 × {cols} 列"),
        ("缺失单元格", f"{_fmt_num(missing)}（{missing / cells * 100:.2f}%）"),
        ("去重删除", f"{_fmt_num(duplicates)} 行"),
        ("异常值", f"{_fmt_num(outliers)} 个"),
        ("填充策略", str(clean_report.get("fill_missing", "—"))),
    ]
    summary = "".join(
        f'<div class="kpi"><div class="kpi-v">{_esc(value)}</div>'
        f'<div class="kpi-l">{_esc(label)}</div></div>'
        for label, value in items
    )
    logs = ""
    if step_logs:
        rows_html = "".join(
            f'<tr><td>{_esc(_fmt_num(rec.get("index", 0)))}</td>'
            f'<td>{_esc(rec.get("op", ""))}</td>'
            f'<td>{_esc(rec.get("status", ""))}</td>'
            f'<td>{_esc(_fmt_num(rec.get("rows_before", 0)))} → '
            f'{_esc(_fmt_num(rec.get("rows_after", 0)))}</td>'
            f'<td>{_esc(rec.get("note", ""))}</td></tr>'
            for rec in step_logs
        )
        logs = (
            '<div class="table-scroll" style="margin-top:14px"><table class="data-table">'
            "<thead><tr><th>#</th><th>算子</th><th>状态</th><th>行数变化</th>"
            f"<th>说明</th></tr></thead><tbody>{rows_html}</tbody></table></div>"
        )
    return f'<div class="kpi-band appendix-kpis">{summary}</div>{logs}'


def _section(title: str, body: str, hint: str = "", *, cls: str = "") -> str:
    if not body:
        return ""
    hint_html = f'<p class="hint">{_esc(hint)}</p>' if hint else ""
    return (
        f'<section class="card report-section {cls}" data-section="{_esc(title)}">'
        f"<h2>{_esc(title)}</h2>{hint_html}{body}</section>"
    )


def _chain_html(rule: Dict[str, Any], step_logs: Optional[List[Dict[str, Any]]]) -> str:
    """把真实执行的步骤渲染成链路，组合场景尤其需要看到阶段顺序。"""
    steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return ""
    status = {}
    for rec in step_logs or []:
        if isinstance(rec, dict):
            status[int(rec.get("index", -1))] = str(rec.get("status", ""))
    items = []
    for index, step in enumerate(steps):
        op = str(step.get("op", ""))
        state = status.get(index, "")
        mark = "" if not state else f' <span class="unit">{_esc(state)}</span>'
        detail = ""
        if op == "filter":
            count = len(step.get("conditions") or [])
            detail = f"（{count} 个条件，{step.get('logic', 'and')}）"
        elif op == "aggregate":
            detail = f"（按 {_fmt(step.get('by'))}）"
        elif op == "date_part":
            detail = f"（{_fmt(step.get('parts'))}）"
        items.append(
            f'<li><code>{_esc(op)}</code>{_esc(str(detail))}{mark}</li>'
        )
    return f'<ol class="chain-list">{"".join(items)}</ol>'


def _contract_html(
    profile: TaskProfile,
    rule: Dict[str, Any],
    step_logs: Optional[List[Dict[str, Any]]],
) -> str:
    """环节契约按本次任务动态生成，不再固定三句话。"""
    ops = [str(s.get("op")) for s in (rule.get("steps") or []) if isinstance(s, dict)]
    label = TASK_LABELS.get(profile.task_type, profile.task_type)
    output_text = {
        "trend": "时间维度的指标趋势、峰值期与末期环比",
        "proportion": "占比指标、分子/分母与构成明细",
        "detail": "逐行明细与清洗前后对照",
        "ranking": "排序后的排名表与头部条目",
        "pivot": "交叉表与透视热力图",
        "join": "关联结果、匹配率与未匹配明细",
        "summary": "分组汇总值与关键指标",
        "distribution": "区间分布与结构占比",
        "correlation": "相关矩阵与散点分布",
    }.get(profile.task_type, "任务结果表与关键指标")
    boxes = [
        ("① 输入", f"{label}需求：{_esc(str(rule.get('intent') or '—'))[:180]}"),
        ("② 执行", "算子链路：" + (" → ".join(ops[:10]) if ops else "无步骤")),
        ("③ 输出", output_text + "；产物落在当前运行目录。"),
    ]
    cards = "".join(
        f'<div class="contract-box"><div class="t">{_esc(title)}</div>'
        f'<div class="d">{body}</div></div>'
        for title, body in boxes
    )
    chain = _chain_html(rule, step_logs)
    return (
        '<details class="fold" open><summary>环节契约 · 本次任务的实际链路</summary>'
        f'<div class="contract-grid">{cards}</div>'
        f'{chain}</details>'
    )


def render_sections(
    profile: TaskProfile,
    rule: Optional[Dict[str, Any]] = None,
    *,
    clean_report: Optional[Dict[str, Any]] = None,
    step_logs: Optional[List[Dict[str, Any]]] = None,
    join_stats: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """按画像的 panels 声明动态编排报告章节，空载荷自动跳过。"""
    rule = rule or {}
    clean_report = clean_report or {}
    panels = [p for p in profile.panels if isinstance(p, dict) and p.get("kind")]
    if not panels:
        panels = [
            {"kind": "kpi", "title": "指标概览", "source": "kpis"},
            {"kind": "chart", "title": "图表", "source": "charts"},
            {"kind": "table", "title": "结果表", "source": "tables"},
        ]
    frame = profile.tables[0].get("frame") if profile.tables else None
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame()
    blocks: List[str] = []
    for panel in panels:
        kind = str(panel.get("kind", ""))
        title = str(panel.get("title", ""))
        hint = str(panel.get("hint", ""))
        if kind == "kpi":
            blocks.append(_section(title, _kpis_html(profile.kpis), hint))
        elif kind == "chart":
            allowed = set(panel.get("chart_kinds") or ())
            charts = [
                c for c in profile.charts
                if not allowed or str(c.get("kind")) in allowed
            ]
            blocks.append(_section(title, _charts_html(charts), hint, cls="chart-section"))
        elif kind == "join":
            if join_stats:
                rows = "".join(
                    f"<li>{_esc(json.dumps(s, ensure_ascii=False))}</li>"
                    for s in join_stats[:5]
                )
                blocks.append(_section(title, f"<ul>{rows}</ul>", hint))
        elif kind == "table":
            note = f"输出：{title}，最多展示 {MAX_REPORT_ROWS} 行。"
            blocks.append(_section(title, _table_html(frame), note or hint))
        elif kind == "note":
            blocks.append(_section(title, f'<p class="conclusion">{_esc(profile.conclusion)}</p>', hint))
    # 任务结论按画像生成，措辞随场景变化
    conclusion = (
        f'<p class="conclusion">{_esc(profile.conclusion or "—")}</p>'
        f'<p class="hint">{_esc(profile.note or "")}</p>'
    )
    blocks.append(_section("任务结论", conclusion))
    aux = _aux_html(profile.tables[1:])
    if aux:
        blocks.append(_section("附表", aux, "维度与关联等补充结果"))
    appendix = _appendix(frame, clean_report, step_logs)
    blocks.append(
        '<details class="fold"><summary>附录 · 数据质量与清洗说明（点击展开）</summary>'
        f"<div>{appendix}</div></details>"
    )
    blocks.append(_contract_html(profile, rule, step_logs))
    return "\n".join(b for b in blocks if b)


def render_task_report(
    profile: TaskProfile,
    run_dir: Path,
    *,
    rule: Optional[Dict[str, Any]] = None,
    join_stats: Optional[List[Dict[str, Any]]] = None,
    clean_report: Optional[Dict[str, Any]] = None,
    step_logs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """渲染任务报告并写入运行目录，返回报告绝对路径（失败返回空串）。"""
    frame = pd.DataFrame()
    if profile.tables:
        candidate = profile.tables[0].get("frame")
        if isinstance(candidate, pd.DataFrame):
            frame = candidate
    rule = rule or {}
    clean_report = clean_report or {}
    label = TASK_LABELS.get(profile.task_type, profile.task_type)
    intent = str(rule.get("intent", "") or "—")
    sources = rule.get("sources") or {}
    source_text = "；".join(
        f"{alias}: {Path(str(src.get('path', ''))).name}"
        for alias, src in sources.items() if isinstance(src, dict)
    ) or Path(str((rule.get("source") or {}).get("path", "—"))).name

    meta_items = [
        ("任务类型", label),
        ("原始需求", intent),
        ("数据源", source_text),
        ("结果规模", f"{_fmt_num(len(frame))} 行 × {len(frame.columns)} 列"),
        ("算子数量", f"{len(rule.get('steps') or [])} 个"),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    if join_stats:
        meta_items.append(("关联统计", f"{len(join_stats)} 次关联"))
    meta = "".join(
        f'<span class="meta-item"><b>{_esc(k)}</b>{_esc(v)}</span>'
        for k, v in meta_items
    )

    css = _style()
    html = _render_template(
        TITLE=_esc(profile.title or "任务报告"),
        KICKER=_esc(f"TASK REPORT · {label}"),
        SUBTITLE=_esc(profile.conclusion or intent),
        BADGE=_esc(f"{label} · {len(profile.kpis)} 项指标 · {len(profile.charts)} 张图表"),
        META=meta,
        SECTIONS=render_sections(
            profile, rule,
            clean_report=clean_report,
            step_logs=step_logs,
            join_stats=join_stats,
        ),
        STYLE=css,
        FOOTER=(
            "DataClean ETL Console · 任务报告（自包含离线单页，可直接打印）· "
            f"运行目录 {run_dir.name}"
        ),
    )
    path = run_dir / f"task_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    path.write_text(html, encoding="utf-8")
    LOGGER.info("任务报告已生成: %s (%d 字节)", path.name, len(html.encode("utf-8")))
    return str(path)

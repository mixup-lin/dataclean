#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 SVG 图表库：零 JavaScript、零第三方绘图依赖，服务端渲染。

所有函数都是「数据 → SVG 字符串」的纯函数，供 Web 结果页与任务报告共用；
尺寸、配色、标签数量都在函数内做上限约束，避免宽表把页面撑爆。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

from etl_pipeline import _esc, _fmt_num, _next_svg_id

PALETTE = [
    "#2563EB", "#0EA5E9", "#6366F1", "#06B6D4", "#8B5CF6",
    "#F59E0B", "#10B981", "#EF4444",
]


def _pair(item: Any) -> Tuple[str, float]:
    try:
        label, value = item[0], item[1]
    except (TypeError, IndexError):
        return str(item), 0.0
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    if math.isnan(num) or math.isinf(num):
        num = 0.0
    return str(label), num


def _short(text: str, n: int = 12) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _svg(width: int, height: int, body: List[str], label: str) -> str:
    return (
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{_esc(label)}">'
        + "".join(body)
        + "</svg>"
    )


def donut(items: Sequence[Any], size: int = 260, label: str = "构成占比") -> str:
    """环形图：最多 7 个扇区，附百分比图例。"""
    pairs = [_pair(i) for i in items][:7]
    if not pairs or sum(v for _, v in pairs) <= 0:
        return ""
    total = sum(v for _, v in pairs) or 1.0
    radius = size * 0.30
    circumference = 2 * math.pi * radius
    cx = cy = size / 2
    body: List[str] = []
    offset = 0.0
    for i, (_label, value) in enumerate(pairs):
        seg = value / total * circumference
        color = PALETTE[i % len(PALETTE)]
        body.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="none" '
            f'stroke="{color}" stroke-width="{size * 0.16:.1f}" '
            f'stroke-dasharray="{seg:.2f} {circumference - seg:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx:.1f} {cy:.1f})">'
            f"<title>{_esc(_label)}: {_fmt_num(value)}（{value / total * 100:.1f}%）</title>"
            "</circle>"
        )
        offset += seg
    body.append(
        f'<text x="{cx:.1f}" y="{cy - 2:.1f}" text-anchor="middle" '
        f'class="donut-total">{_esc(_fmt_num(total))}</text>'
        f'<text x="{cx:.1f}" y="{cy + 16:.1f}" text-anchor="middle" '
        f'class="donut-sub">合计</text>'
    )
    legend = []
    for i, (name, value) in enumerate(pairs):
        y = 26 + i * 26
        legend.append(
            f'<rect x="{size + 4}" y="{y - 10}" width="10" height="10" rx="3" '
            f'fill="{PALETTE[i % len(PALETTE)]}"/>'
            f'<text x="{size + 20}" y="{y}" class="legend-text">'
            f"{_esc(_short(name, 10))} · {value / total * 100:.1f}%</text>"
        )
    return _svg(size + 190, size, body + legend, label)


def hbar(
    items: Sequence[Any], label: str = "排名", width: int = 620, limit: int = 12
) -> str:
    """横向条形图：适合类目排名与 TopN。"""
    pairs = [(_p[0], _p[1]) for _p in (_pair(i) for i in items)][:limit]
    if not pairs:
        return ""
    top = max((abs(v) for _, v in pairs), default=0) or 1.0
    row_h, gap, left, right = 26, 10, 116, 78
    height = len(pairs) * (row_h + gap) + 18
    body: List[str] = []
    for i, (name, value) in enumerate(pairs):
        y = 12 + i * (row_h + gap)
        w = max(2.0, (abs(value) / top) * (width - left - right))
        color = PALETTE[i % len(PALETTE)]
        body.append(
            f'<text x="{left - 10}" y="{y + row_h * 0.68:.1f}" text-anchor="end" '
            f'class="axis-text">{_esc(_short(name, 9))}</text>'
            f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{row_h}" rx="6" '
            f'fill="{color}" fill-opacity="0.88">'
            f"<title>{_esc(name)}: {_fmt_num(value)}</title></rect>"
            f'<text x="{left + w + 8:.1f}" y="{y + row_h * 0.68:.1f}" '
            f'class="value-text">{_esc(_fmt_num(value))}</text>'
        )
    return _svg(width, height, body, label)


def pareto(
    items: Sequence[Any], label: str = "帕累托图", width: int = 640
) -> str:
    """帕累托图：降序柱 + 累计占比折线，双轴。"""
    pairs = sorted((_pair(i) for i in items), key=lambda p: p[1], reverse=True)[:12]
    if not pairs:
        return ""
    total = sum(v for _, v in pairs) or 1.0
    top = max((v for _, v in pairs), default=0) or 1.0
    height, pad_top, pad_bottom = 300, 20, 46
    left, right = 48, 48
    plot_h = height - pad_top - pad_bottom
    plot_w = width - left - right
    step = plot_w / len(pairs)
    bar_w = step * 0.56
    body: List[str] = []
    for tick in range(5):
        y = pad_top + plot_h * tick / 4
        body.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'class="grid-line"/>'
        )
    cumulative = 0.0
    points: List[Tuple[float, float]] = []
    ratios: List[float] = []
    for i, (name, value) in enumerate(pairs):
        cumulative += value
        x = left + i * step + (step - bar_w) / 2
        h = value / top * plot_h
        body.append(
            f'<rect x="{x:.1f}" y="{pad_top + plot_h - h:.1f}" width="{bar_w:.1f}" '
            f'height="{max(h, 1):.1f}" rx="5" fill="#2563EB" fill-opacity="0.86">'
            f"<title>{_esc(name)}: {_fmt_num(value)}</title></rect>"
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 16}" text-anchor="middle" '
            f'class="axis-text">{_esc(_short(name, 6))}</text>'
        )
        ratio = cumulative / total * 100
        ratios.append(ratio)
        points.append((x + bar_w / 2, pad_top + plot_h * (1 - ratio / 100)))
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    body.append(
        f'<polyline points="{path}" fill="none" stroke="#F59E0B" stroke-width="2.4" '
        f'stroke-linejoin="round"/>'
    )
    for (x, y), ratio in zip(points, ratios):
        body.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="#fff" '
            f'stroke="#F59E0B" stroke-width="2"/>'
            f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" '
            f'class="value-text">{ratio:.0f}%</text>'
        )
    body.append(
        f'<text x="{left}" y="{pad_top - 4}" class="axis-text">柱：数值 ｜ 线：累计占比</text>'
    )
    return _svg(width, height, body, label)


def line(points: Sequence[Any], label: str = "趋势", width: int = 640) -> str:
    """折线 + 面积图：适合时间趋势。"""
    pairs = [_pair(p) for p in points][:36]
    if len(pairs) < 2:
        return hbar(pairs, label=label, width=width)
    values = [v for _, v in pairs]
    vmax, vmin = max(values), min(values)
    if vmax == vmin:
        vmax, vmin = vmax + 1, min(vmin, 0)
    height, pad_top, pad_bottom, pad_x = 280, 18, 44, 44
    plot_h = height - pad_top - pad_bottom
    plot_w = width - pad_x * 2
    step = plot_w / max(len(pairs) - 1, 1)
    coords = [
        (
            pad_x + i * step,
            pad_top + plot_h * (1 - (v - vmin) / (vmax - vmin or 1)),
        )
        for i, v in enumerate(values)
    ]
    body: List[str] = []
    for tick in range(4):
        y = pad_top + plot_h * tick / 3
        body.append(
            f'<line x1="{pad_x}" y1="{y:.1f}" x2="{width - pad_x}" y2="{y:.1f}" '
            f'class="grid-line"/>'
        )
    gradient = _next_svg_id("area")
    body.append(
        f'<defs><linearGradient id="{gradient}" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#2563EB" stop-opacity="0.32"/>'
        '<stop offset="1" stop-color="#2563EB" stop-opacity="0.02"/>'
        "</linearGradient></defs>"
    )
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = (
        f"{coords[0][0]:.1f},{pad_top + plot_h:.1f} " + path + " "
        f"{coords[-1][0]:.1f},{pad_top + plot_h:.1f}"
    )
    body.append(f'<polygon points="{area}" fill="url(#{gradient})"/>')
    body.append(
        f'<polyline points="{path}" fill="none" stroke="#2563EB" stroke-width="2.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    label_step = max(1, len(pairs) // 8)
    for i, ((name, value), (x, y)) in enumerate(zip(pairs, coords)):
        body.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#fff" '
            f'stroke="#2563EB" stroke-width="2">'
            f"<title>{_esc(name)}: {_fmt_num(value)}</title></circle>"
        )
        if i % label_step == 0 or i == len(pairs) - 1:
            body.append(
                f'<text x="{x:.1f}" y="{height - 14}" text-anchor="middle" '
                f'class="axis-text">{_esc(_short(name, 7))}</text>'
            )
    body.append(
        f'<text x="{pad_x}" y="{pad_top - 2}" class="axis-text">'
        f"最高 {_fmt_num(vmax)} ／ 最低 {_fmt_num(vmin)}</text>"
    )
    return _svg(width, height, body, label)


def column(items: Sequence[Any], label: str = "分布", width: int = 620) -> str:
    """纵向柱状图：适合分箱分布与计数对比。"""
    pairs = [_pair(i) for i in items][:16]
    if not pairs:
        return ""
    top = max((v for _, v in pairs), default=0) or 1.0
    height, pad_top, pad_bottom, pad_x = 280, 18, 52, 40
    plot_h = height - pad_top - pad_bottom
    plot_w = width - pad_x * 2
    step = plot_w / len(pairs)
    bar_w = step * 0.6
    body: List[str] = []
    for tick in range(4):
        y = pad_top + plot_h * tick / 3
        body.append(
            f'<line x1="{pad_x}" y1="{y:.1f}" x2="{width - pad_x}" y2="{y:.1f}" '
            f'class="grid-line"/>'
        )
    for i, (name, value) in enumerate(pairs):
        h = value / top * plot_h
        x = pad_x + i * step + (step - bar_w) / 2
        body.append(
            f'<rect x="{x:.1f}" y="{pad_top + plot_h - h:.1f}" width="{bar_w:.1f}" '
            f'height="{max(h, 1):.1f}" rx="5" fill="{PALETTE[i % len(PALETTE)]}" '
            f'fill-opacity="0.86"><title>{_esc(name)}: {_fmt_num(value)}</title></rect>'
            f'<text x="{x + bar_w / 2:.1f}" y="{pad_top + plot_h - h - 6:.1f}" '
            f'text-anchor="middle" class="value-text">{_esc(_fmt_num(value))}</text>'
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 18}" text-anchor="middle" '
            f'class="axis-text">{_esc(_short(name, 9))}</text>'
        )
    return _svg(width, height, body, label)


def scatter(
    points: Sequence[Any], x_label: str = "X", y_label: str = "Y",
    label: str = "散点图", width: int = 620,
) -> str:
    """散点图：最多 400 个点，附坐标轴与相关系数提示。"""
    data: List[Tuple[float, float, str]] = []
    for item in list(points)[:400]:
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isnan(x) or math.isnan(y):
            continue
        name = str(item[2]) if len(item) > 2 else ""
        data.append((x, y, name))
    if len(data) < 2:
        return ""
    xs = [p[0] for p in data]
    ys = [p[1] for p in data]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax == xmin:
        xmax = xmin + 1
    if ymax == ymin:
        ymax = ymin + 1
    height, pad = 300, 46
    body: List[str] = []
    for tick in range(4):
        y = pad + (height - pad * 2) * tick / 3
        body.append(
            f'<line x1="{pad}" y1="{y:.1f}" x2="{width - pad}" y2="{y:.1f}" '
            f'class="grid-line"/>'
        )
    for x, y, name in data:
        cx = pad + (x - xmin) / (xmax - xmin) * (width - pad * 2)
        cy = height - pad - (y - ymin) / (ymax - ymin) * (height - pad * 2)
        body.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="#0EA5E9" '
            f'fill-opacity="0.7" stroke="#0369A1" stroke-width="1">'
            f"<title>{_esc(name)} ({_fmt_num(x)}, {_fmt_num(y)})</title></circle>"
        )
    body.append(
        f'<text x="{width / 2:.1f}" y="{height - 10}" text-anchor="middle" '
        f'class="axis-text">{_esc(x_label)}</text>'
        f'<text x="12" y="{height / 2:.1f}" class="axis-text" '
        f'transform="rotate(-90 12 {height / 2:.1f})">{_esc(y_label)}</text>'
    )
    return _svg(width, height, body, label)


def heatmap(
    rows: Sequence[str], cols: Sequence[str], matrix: Sequence[Sequence[float]],
    label: str = "热力图",
) -> str:
    """交叉表热力图：行 / 列均做上限截断。"""
    rows = [str(r) for r in list(rows)[:14]]
    cols = [str(c) for c in list(cols)[:10]]
    if not rows or not cols:
        return ""
    values: List[List[float]] = []
    for r in range(len(rows)):
        line: List[float] = []
        for c in range(len(cols)):
            try:
                v = float(matrix[r][c])
            except (IndexError, TypeError, ValueError):
                v = 0.0
            if math.isnan(v) or math.isinf(v):
                v = 0.0
            line.append(v)
        values.append(line)
    flat = [v for line in values for v in line]
    vmax = max((abs(v) for v in flat), default=0) or 1.0
    cell_w, cell_h, left, top = 58, 26, 118, 34
    width = left + cell_w * len(cols) + 16
    height = top + cell_h * len(rows) + 22
    body: List[str] = []
    for j, name in enumerate(cols):
        body.append(
            f'<text x="{left + j * cell_w + cell_w / 2:.1f}" y="{top - 10}" '
            f'text-anchor="middle" class="axis-text">{_esc(_short(name, 6))}</text>'
        )
    for i, name in enumerate(rows):
        y = top + i * cell_h
        body.append(
            f'<text x="{left - 8}" y="{y + cell_h * 0.66:.1f}" text-anchor="end" '
            f'class="axis-text">{_esc(_short(name, 8))}</text>'
        )
        for j in range(len(cols)):
            v = values[i][j]
            ratio = abs(v) / vmax
            body.append(
                f'<rect x="{left + j * cell_w}" y="{y}" width="{cell_w - 2}" '
                f'height="{cell_h - 2}" rx="4" fill="#2563EB" '
                f'fill-opacity="{0.08 + ratio * 0.82:.2f}">'
                f"<title>{_esc(rows[i])} × {_esc(cols[j])}: {_fmt_num(v)}</title></rect>"
            )
            if ratio > 0.45:
                body.append(
                    f'<text x="{left + j * cell_w + (cell_w - 2) / 2:.1f}" '
                    f'y="{y + cell_h * 0.66:.1f}" text-anchor="middle" '
                    f'class="cell-text-light">{_esc(_fmt_num(v))}</text>'
                )
    return _svg(width, height, body, label)


def _heatmap_from_pivot(chart: Dict[str, Any]) -> str:
    rows = chart.get("rows") or []
    cols = chart.get("cols") or []
    matrix = chart.get("matrix") or []
    return heatmap(rows, cols, matrix, chart.get("title", "交叉表"))


def render_chart(chart: Dict[str, Any]) -> str:
    """按 kind 分派渲染，供 Web 结果页与任务报告复用。"""
    kind = str(chart.get("kind") or "hbar")
    title = str(chart.get("title") or "")
    data = chart.get("data") or []
    if kind == "donut":
        return donut(data, label=title)
    if kind == "pareto":
        return pareto(data, label=title)
    if kind == "line":
        return line(data, label=title)
    if kind == "column":
        return column(data, label=title)
    if kind == "scatter":
        return scatter(
            chart.get("points") or [],
            x_label=str(chart.get("x_label") or "X"),
            y_label=str(chart.get("y_label") or "Y"),
            label=title,
        )
    if kind == "heatmap":
        return _heatmap_from_pivot(chart)
    return hbar(data, label=title)

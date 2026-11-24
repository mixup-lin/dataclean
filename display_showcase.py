#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""展示模板样例生成器。

用真实数据 + 真实 :func:`task_profile.build_profile` 画像，按 :mod:`display_templates`
的模板引擎生成「演示 / 汇报 / 使用说明」三类自包含 HTML，以及一页总览目录，
可直接用于演示、汇报与后续扩展参考。

用法：:

    python display_showcase.py --output-dir output/showcase

生成的 HTML 均为离线单页（内联样式与 SVG），浏览器直接打开即可。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

import display_templates as dt
from task_profile import build_profile


@dataclass
class ShowcaseCase:
    """一个展示样例：场景名、数据、规则与目标说明。"""

    key: str
    title: str
    frame: pd.DataFrame
    rule: Dict[str, Any]
    narrative: str
    template: Optional[str] = None
    clean_report: Optional[Dict[str, Any]] = None
    join_stats: Optional[List[Dict[str, Any]]] = None
    step_logs: Optional[List[Dict[str, Any]]] = None


def _sample_cases() -> List[ShowcaseCase]:
    """构造覆盖各典型场景的样例（数据 + 规则均为真实可执行结构）。"""
    cases: List[ShowcaseCase] = []

    # 1) 分组汇总
    group_df = pd.DataFrame({
        "区县": ["鼓楼区", "台江区", "仓山区", "马尾区", "晋安区", "长乐区"],
        "成交套数": [120, 86, 74, 35, 58, 22],
        "成交总额(万)": [36000.0, 21500.0, 18200.0, 8400.0, 13900.0, 5200.0],
    })
    cases.append(ShowcaseCase(
        key="group_summary", title="分组汇总 · 各区县成交对比",
        frame=group_df,
        rule={"task_type": "summary", "intent": "按区县汇总成交套数与成交总额",
              "steps": [{"op": "aggregate", "by": ["区县"],
                         "column": ["成交套数", "成交总额(万)"], "func": ["sum", "sum"]}]},
        narrative="按区县聚合后，卡片高亮头部区县，条形图对比成交量，表格承载完整分组结果。",
    ))

    # 2) 趋势对比（跨年月均价）
    trend_df = pd.DataFrame({
        "结束时间_年月": ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"],
        "成交单价": [4.82, 4.95, 5.10, 4.98, 5.23, 5.41],
    })
    cases.append(ShowcaseCase(
        key="trend", title="趋势对比 · 2026 年月均价走势",
        frame=trend_df,
        rule={"task_type": "trend", "intent": "2026 年按月的平均成交单价趋势",
              "steps": [
                  {"op": "date_part", "column": "结束时间", "parts": ["year", "month"]},
                  {"op": "aggregate", "by": ["结束时间_年月"],
                   "column": ["成交价(元)"], "func": ["mean"], "alias": ["成交单价"]},
              ]},
        narrative="折线图呈现价格走势，卡片标注峰值期与末期环比，表格逐月列出均价。",
    ))

    # 3) 排名分析
    rank_df = pd.DataFrame({
        "小区": ["融侨锦江", "保利天悦", "中海金筑", "万科金域", "龙湖天璞", "融创中心"],
        "成交套数": [58, 46, 39, 31, 27, 19],
    })
    cases.append(ShowcaseCase(
        key="ranking", title="排名分析 · 小区成交热度 Top6",
        frame=rank_df,
        rule={"task_type": "ranking", "intent": "按小区统计成交套数并降序排名",
              "steps": [
                  {"op": "aggregate", "by": ["小区"], "column": "成交套数", "func": "sum"},
                  {"op": "sort", "by": "成交套数", "ascending": [False]},
              ]},
        narrative="横向条形图呈现名次，卡片标注冠军与前 3 集中度，表格为排名明细。",
    ))

    # 4) 占比构成（成交率）
    rate_df = pd.DataFrame({
        "区县": ["鼓楼区", "台江区", "仓山区"],
        "成交数": [72, 51, 40],
        "未成交数": [28, 49, 60],
    })
    cases.append(ShowcaseCase(
        key="proportion", title="占比构成 · 各区县成交率",
        frame=rate_df,
        rule={"task_type": "proportion", "intent": "各区县成交率",
              "steps": [{"op": "rate", "by": ["区县"], "true_label": "成交",
                         "false_label": "未成交", "numerator": "hit",
                         "denominator": "total", "alias": "成交率"}]},
        narrative="环形图展示成交 / 未成交构成，卡片给出成交率与分子分母，表格保留占比明细。",
    ))

    # 5) 分布分析
    dist_df = pd.DataFrame({
        "面积区间": ["<50㎡", "50-90㎡", "90-120㎡", "120-150㎡", "≥150㎡"],
        "样本数": [42, 118, 96, 55, 23],
    })
    cases.append(ShowcaseCase(
        key="distribution", title="分布分析 · 成交面积区间结构",
        frame=dist_df,
        rule={"task_type": "distribution", "intent": "按面积区间统计样本数分布",
              "steps": [{"op": "aggregate", "by": ["面积区间"],
                         "column": "样本数", "func": "sum"}]},
        narrative="柱状图展示区间分布，卡片高亮主区间，表格列出各区间计数。",
    ))

    # 6) 透视分析
    pivot_df = pd.DataFrame({
        "区县": ["鼓楼区", "台江区", "仓山区"],
        "2026Q1": [3200.0, 2100.0, 1800.0],
        "2026Q2": [3550.0, 2280.0, 1960.0],
        "2026Q3": [3780.0, 2410.0, 2050.0],
    })
    cases.append(ShowcaseCase(
        key="pivot", title="透视分析 · 区县 × 季度成交总额",
        frame=pivot_df,
        rule={"task_type": "pivot", "intent": "区县按季度的成交总额交叉表",
              "steps": [{"op": "pivot", "index": "区县", "columns": "季度",
                         "values": "成交总额(万)"}]},
        narrative="热力图呈现交叉热点，表格保留透视原表，卡片给出交叉规模。",
    ))

    # 7) 相关分析
    corr_df = pd.DataFrame({
        "面积(㎡)": [58, 72, 91, 105, 120, 138, 85, 96],
        "成交价(万)": [285, 352, 448, 512, 598, 672, 421, 470],
    })
    cases.append(ShowcaseCase(
        key="correlation", title="相关分析 · 面积与成交价关系",
        frame=corr_df,
        rule={"task_type": "correlation", "intent": "分析面积与成交价的相关关系",
              "steps": [{"op": "aggregate", "by": ["面积(㎡)"],
                         "column": "成交价(万)", "func": "mean"}]},
        narrative="散点图呈现两变量关系，卡片给出相关系数，表格保留数值明细。",
    ))

    # 8) 多表合并
    join_df = pd.DataFrame({
        "小区": ["融侨锦江", "保利天悦", "中海金筑", "万科金域"],
        "成交套数": [58, 46, 39, 31],
        "均价(万)": [5.2, 5.6, 4.9, 4.7],
    })
    cases.append(ShowcaseCase(
        key="join", title="多表合并 · 成交与均价关联",
        frame=join_df,
        rule={"task_type": "join", "intent": "关联成交明细与小区均价",
              "steps": [{"op": "join", "how": "left", "left_on": "小区", "right_on": "小区"}]},
        narrative="关联质量卡展示匹配率，卡片给出关联规模，表格保留合并结果。",
        join_stats=[{"alias": "均价表", "how": "left", "matched": 4,
                     "match_rate": 100.0, "unmatched_left": 0}],
    ))

    # 9) 异常识别
    anomaly_df = pd.DataFrame({
        "标的号": ["A001", "A002", "A003", "A004", "A005"],
        "成交价(万)": [285, 352, 448, 512, 9800],
    })
    cases.append(ShowcaseCase(
        key="anomaly", title="异常识别 · 成交价异常点",
        frame=anomaly_df,
        rule={"task_type": "detail", "intent": "识别成交价异常值",
              "steps": [{"op": "outlier", "column": "成交价(万)",
                         "method": "iqr", "action": "flag"}]},
        narrative="卡片给出异常规模，表格列出异常记录，附录展示检测边界与处置动作。",
        clean_report={"outliers": {"count": 1, "action": "flag",
                                   "bounds": {"成交价(万)": (100.0, 800.0)}},
                      "duplicates_removed": 0, "fill_missing": "none"},
    ))

    # 10) 数据清洗
    clean_df = pd.DataFrame({
        "标的号": ["A001", "A002", "A003", "A004"],
        "区县": ["鼓楼区", "台江区", None, "仓山区"],
        "成交价(万)": [285, 352, 448, 512],
    })
    cases.append(ShowcaseCase(
        key="cleaning", title="数据清洗 · 去重与空值处置",
        frame=clean_df,
        rule={"task_type": "detail", "intent": "按标的号去重并填充区县空值",
              "steps": [{"op": "dedupe", "keys": ["标的号"], "keep": "first"},
                        {"op": "fill", "columns": ["区县"], "value": "未知"}]},
        narrative="卡片展示清洗规模，表格列出处置后明细，附录给出列级质量基线。",
        clean_report={"duplicates_removed": 2, "fill_missing": "value:未知",
                      "outliers": {"count": 0}},
        step_logs=[
            {"index": 0, "op": "dedupe", "status": "ok", "rows_before": 6, "rows_after": 4,
             "note": "按标的号去重"},
            {"index": 1, "op": "fill", "status": "ok", "rows_before": 4, "rows_after": 4,
             "note": "区县空值填充为未知"},
        ],
    ))

    # 11) 明细查看
    detail_df = pd.DataFrame({
        "标的号": ["A001", "A002", "A003"],
        "区县": ["鼓楼区", "台江区", "鼓楼区"],
        "成交价(万)": [285, 352, 448],
    })
    cases.append(ShowcaseCase(
        key="detail", title="明细查看 · 鼓楼区成交记录",
        frame=detail_df,
        rule={"task_type": "detail", "intent": "提取区县为鼓楼区的成交记录",
              "steps": [{"op": "filter", "conditions": [
                  {"column": "区县", "operator": "eq", "value": "鼓楼区"}]}]},
        narrative="以表格为主体逐行查看，卡片给出结果规模，不渲染聚合图表。",
    ))

    return cases


def _context(case: ShowcaseCase, goal: str) -> dt.DisplayContext:
    profile = build_profile(
        case.frame, case.rule,
        clean_report=case.clean_report or {},
        join_stats=case.join_stats or [],
    )
    return dt.DisplayContext(
        profile=profile,
        rule=case.rule,
        frame=case.frame,
        clean_report=case.clean_report or {},
        step_logs=case.step_logs or [],
        join_stats=case.join_stats or [],
    )


def render_case_html(case: ShowcaseCase, goal: str) -> str:
    """渲染单个样例在指定目标下的完整 HTML 文档。"""
    ctx = _context(case, goal)
    # 样例显式指定场景模板（同时也是「配置展示模板」能力的演示）
    plan = dt.build_plan(ctx, goal=goal, template=case.template or case.key)
    # 使用说明目标：保证包含算子链路块
    if goal == "guide" and not any(b.kind == "chain" for b in plan.blocks):
        plan.blocks.append(dt.BlockSpec("chain", "算子链路", "本次样例实际执行的算子顺序",
                                        "chain"))

    explanation = dt.plan_explanation(plan).replace("\n", "<br>")
    ctx_note = (
        f'<p class="conclusion">{dt.esc(case.narrative)}</p>'
        f'<details class="source-box" open><summary>展示计划说明（场景与目标）</summary>'
        f'<div class="hint">{explanation}</div></details>'
    )
    meta = "".join(
        f'<span class="meta-item"><b>{dt.esc(k)}</b>{dt.esc(v)}</span>'
        for k, v in [
            ("场景", dt.SCENARIO_CATALOG[plan.scenario].label),
            ("展示目标", dt.GOAL_LABELS[plan.goal]),
            ("结果规模", f"{dt.fmt_num(len(case.frame))} 行 × {len(case.frame.columns)} 列"),
            ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ]
    )
    sections = ctx_note + "\n" + dt.render_sections(plan, ctx)
    conclusion = (
        f'<section class="card report-section"><h2>任务结论</h2>'
        f'<p class="conclusion">{dt.esc(ctx.conclusion or "—")}</p></section>'
    )
    template = (dt.PAGES / "report.html").read_text(encoding="utf-8")
    values = {
        "TITLE": dt.esc(case.title),
        "KICKER": dt.esc(f"DISPLAY SHOWCASE · {dt.GOAL_LABELS[goal]}"),
        "SUBTITLE": dt.esc(case.narrative),
        "BADGE": dt.esc(f"{dt.SCENARIO_CATALOG[plan.scenario].label} · {dt.GOAL_LABELS[goal]}"),
        "META": meta,
        "SECTIONS": sections + "\n" + conclusion,
        "STYLE": dt._inline_style(),
        "FOOTER": "DataClean ETL Console · 场景化展示模板样例 · 自包含离线单页",
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def render_index_html(cases: List[ShowcaseCase]) -> str:
    """生成目录页：系统梳理「场景 → 展示方式」映射并链接各样例。"""
    rows: List[str] = []
    for case in cases:
        spec = dt.SCENARIO_CATALOG.get(case.template or case.key)
        if spec is None:
            # 用画像推导场景
            ctx = _context(case, "report")
            spec = dt.SCENARIO_CATALOG[dt.build_plan(ctx).scenario]
        blocks = "、".join(dt.BLOCK_LABELS.get(k, k) for k in spec.blocks)
        charts = "、".join(spec.chart_kinds) if spec.chart_kinds else "不渲染图表"
        links = " ｜ ".join(
            f'<a href="{case.key}_{goal}.html">{dt.GOAL_LABELS[goal]}</a>'
            for goal in ("demo", "report", "guide")
        )
        rows.append(
            f"<tr><td>{dt.esc(spec.label)}</td><td>{dt.esc(spec.description)}</td>"
            f"<td>{dt.esc(blocks)}</td><td>{dt.esc(charts)}</td>"
            f"<td>{dt.esc(case.title)}</td><td>{links}</td></tr>"
        )
    table = (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        "<th>场景</th><th>说明</th><th>推荐展示块</th><th>适用图表</th>"
        "<th>样例</th><th>查看</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
    )
    principles = (
        '<div class="card"><h2>模板选择原则</h2>'
        '<ol class="chain-list">'
        "<li><b>数据类型</b>：数值字段 → 直方图 / 散点；类别字段 → 条形 / 环形；时间字段 → 折线。</li>"
        "<li><b>字段特征</b>：低基数字段适合构成图，高基数 ID 类字段跳过图表只入表格。</li>"
        "<li><b>结果规模</b>：小规模保留完整明细，中大规模收紧表格行数、图表只用聚合后小数据。</li>"
        "<li><b>展示目标</b>：演示突出卡片与图表、汇报补齐结论与附录、使用说明追加算子链路与选择理由。</li>"
        "</ol></div>"
    )
    meta = "".join(
        f'<span class="meta-item"><b>{dt.esc(k)}</b>{dt.esc(v)}</span>'
        for k, v in [
            ("覆盖场景", f"{len(cases)} 类"),
            ("展示目标", "演示 / 汇报 / 使用说明"),
            ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ]
    )
    template = (dt.PAGES / "report.html").read_text(encoding="utf-8")
    values = {
        "TITLE": "数据处理场景与展示方式总览",
        "KICKER": "DISPLAY TEMPLATE CATALOG",
        "SUBTITLE": "系统梳理典型数据处理场景与适配的展示方式，点击样例查看演示 / 汇报 / 使用说明三种模板。",
        "BADGE": f"{len(cases)} 类场景 · 3 种展示目标",
        "META": meta,
        "SECTIONS": principles + '<section class="card report-section"><h2>场景 → 展示方式映射</h2>' + table + "</section>",
        "STYLE": dt._inline_style(),
        "FOOTER": "DataClean ETL Console · 场景化展示模板总览",
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def generate_showcase(output_dir: Path) -> List[Path]:
    """生成总览页 + 每个样例的三类 HTML，返回生成的文件路径列表。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = _sample_cases()
    written: List[Path] = []
    index = output_dir / "index.html"
    index.write_text(render_index_html(cases), encoding="utf-8")
    written.append(index)
    for case in cases:
        for goal in ("demo", "report", "guide"):
            path = output_dir / f"{case.key}_{goal}.html"
            path.write_text(render_case_html(case, goal), encoding="utf-8")
            written.append(path)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="display_showcase.py", description="生成场景化展示模板样例 HTML"
    )
    parser.add_argument("--output-dir", default="output/showcase", help="输出目录")
    args = parser.parse_args(argv)
    paths = generate_showcase(Path(args.output_dir))
    print(f"已生成 {len(paths)} 个展示样例 HTML：{args.output_dir}")
    for path in paths[:5]:
        print(f"  - {path}")
    if len(paths) > 5:
        print(f"  ... 其余 {len(paths) - 5} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

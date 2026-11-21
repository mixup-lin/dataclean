"""结果页面板化：规格派生、序列化契约与渲染回退（不需要网络与真实产物）。"""
from __future__ import annotations

import pandas as pd

from task_profile import _derive_panels, build_profile, fallback_profile
from webapp import _panels_html

PAGE = {
    "columns": ["小区", "合计"],
    "rows": [{"小区": "A", "合计": "3"}],
    "total": 1,
    "page": 1,
    "pages": 1,
}
EMPTY_PAGE = {"columns": [], "rows": [], "total": 0, "page": 1, "pages": 1}
PANEL_KEYS = {"kind", "title", "hint", "source", "chart_kinds", "count"}


def _panel(kind: str, chart_kinds=()) -> dict:
    return {
        "kind": kind, "title": kind, "hint": "", "source": "",
        "chart_kinds": list(chart_kinds), "count": 0,
    }


def _task(**kwargs) -> dict:
    task = {
        "kpis": [{"label": "结果行数", "value": 1, "hint": "", "unit": ""}],
        "charts": [], "tables": [], "join": [], "panels": [],
        "conclusion": "", "note": "",
    }
    task.update(kwargs)
    return task


def _render(task: dict, page=PAGE, name: str = "结果表") -> str:
    return _panels_html(task, {}, "", "run-1", page_data=page, table_name=name)


# ============================================================
# 面板规格派生
# ============================================================
def test_derive_panels_follow_task_type():
    expected = {
        "proportion": ["kpi", "table"],
        "ranking": ["kpi", "chart", "table"],
        "trend": ["kpi", "chart", "table"],
        "distribution": ["kpi", "chart", "table"],
        "pivot": ["kpi", "chart", "table"],
        "summary": ["kpi", "chart", "table"],
        "correlation": ["kpi", "chart", "table"],
        "detail": ["kpi", "table"],
    }
    for task_type, kinds in expected.items():
        panels = _derive_panels(task_type, ())
        assert [p["kind"] for p in panels] == kinds, task_type
        assert all(set(p) == PANEL_KEYS for p in panels), task_type


def test_derive_panels_join_first_then_table():
    panels = _derive_panels("join", ("join", "aggregate"))
    assert [p["kind"] for p in panels] == ["kpi", "join", "table"]
    assert panels[1]["source"] == "join_stats"


def test_derive_panels_adds_heatmap_for_pivot_op():
    panels = _derive_panels("summary", ("aggregate", "pivot"))
    assert [p["kind"] for p in panels] == ["kpi", "chart", "chart", "table"]
    assert panels[2]["chart_kinds"] == ["heatmap"]
    # pivot 任务本身已含热力图，不重复追加
    assert [p["kind"] for p in _derive_panels("pivot", ("pivot",))] == ["kpi", "chart", "table"]


def test_derive_panels_chart_kinds_filtered_per_task():
    for task_type in ("ranking", "trend", "distribution", "correlation"):
        chart = next(p for p in _derive_panels(task_type, ()) if p["kind"] == "chart")
        assert chart["chart_kinds"], task_type


# ============================================================
# 序列化契约
# ============================================================
RULE_RANKING = {
    "steps": [
        {"op": "aggregate", "by": "小区", "column": "成交价(元)", "func": "sum"},
        {"op": "sort", "by": "成交价(元)", "order": "desc"},
    ]
}


def test_to_dict_exposes_panels():
    frame = pd.DataFrame({"小区": ["A", "B", "B"], "成交价(元)": [1.0, 2.0, 3.0]})
    profile = build_profile(frame, RULE_RANKING)
    data = profile.to_dict()
    assert data["panels"] == profile.panels
    assert [p["kind"] for p in data["panels"]] == ["kpi", "chart", "table"]
    assert {"task_type", "kpis", "charts", "tables", "note"} <= set(data)


def test_fallback_profile_keeps_panel_contract():
    profile = fallback_profile(pd.DataFrame())
    assert profile.panels and profile.panels[0]["kind"] == "kpi"
    assert profile.to_dict()["panels"] == profile.panels


# ============================================================
# 渲染：分派、过滤、回退
# ============================================================
def test_panels_html_renders_kpi_and_table():
    html = _render(_task(panels=[_panel("kpi"), _panel("table")]))
    assert 'class="panel-flow"' in html
    assert "panel-kpi" in html and "kpi-band" in html
    assert "panel-table" in html and "data-table" in html
    assert "table=0" in html and "page=1" in html


def test_panels_html_chart_panel_filters_kinds():
    charts = [
        {"kind": "hbar", "title": "条形图标题", "data": [["A", 1]]},
        {"kind": "donut", "title": "环形图标题", "data": [["A", 1], ["B", 2]]},
    ]
    html = _render(_task(charts=charts, panels=[_panel("chart", ("donut",))]))
    assert "环形图标题" in html
    assert "条形图标题" not in html


def test_panels_html_skips_empty_payload():
    task = _task(kpis=[], panels=[_panel("kpi"), _panel("join"), _panel("note")])
    assert _render(task, page=EMPTY_PAGE, name="") == ""


def test_panels_html_falls_back_when_spec_missing():
    html = _render(_task(tables=[{"name": "主结果", "note": "", "rows": 1}]))
    assert "panel-kpi" in html and "panel-table" in html
    assert "panel-chart" not in html  # 无图表载荷时默认组合按需舍弃


def test_join_panel_shows_match_quality():
    html = _render(_task(
        join=[{
            "alias": "extra1", "how": "left", "matched": 80,
            "match_rate": 93.5, "unmatched_left": 6,
        }],
        panels=[_panel("join")],
    ))
    assert "panel-join" in html and "join-grid" in html
    assert "extra1" in html and "left join" in html
    assert "匹配率" in html and "主表未匹配" in html
    assert "80" in html and "93.5" in html


def test_table_panel_handles_missing_page_data():
    task = _task(tables=[{"name": "主结果", "note": "", "rows": 2}])
    html = _panels_html(task, {}, "不存在的输出目录", "run-1", 0, 1)
    assert "panel-table" in html
    assert "结果 CSV 不可读" in html or "data-table" in html


def test_unknown_panel_kind_is_ignored():
    html = _render(_task(panels=[_panel("unknown")]))
    assert "panel-unknown" not in html
    assert "panel-kpi" in html  # 全部被跳过时回退默认组合

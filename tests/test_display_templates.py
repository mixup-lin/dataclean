"""场景化展示模板引擎：场景识别、配置选择、块渲染与文档输出（离线）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import display_templates as dt
from task_profile import build_profile


def _ctx(frame, rule, *, clean_report=None, join_stats=None, step_logs=None):
    profile = build_profile(frame, rule, clean_report=clean_report or {},
                            join_stats=join_stats or [])
    return dt.DisplayContext(
        profile=profile, rule=rule, frame=frame,
        clean_report=clean_report or {}, join_stats=join_stats or [],
        step_logs=step_logs or [],
    )


# ============================================================
# 场景目录完整性
# ============================================================
def test_catalog_covers_typical_scenarios():
    for key in ("cleaning", "statistics", "group_summary", "trend",
                "anomaly", "proportion", "ranking", "distribution",
                "correlation", "pivot", "join", "detail"):
        spec = dt.SCENARIO_CATALOG[key]
        assert spec.label and spec.description
        assert spec.blocks and "table" in spec.blocks
        assert "note" in spec.blocks or "quality" in spec.blocks


def test_block_labels_defined_for_every_catalog_block():
    kinds = {k for spec in dt.SCENARIO_CATALOG.values() for k in spec.blocks}
    assert kinds <= set(dt.BLOCK_LABELS)


# ============================================================
# 场景识别（数据类型 / 字段特征 / 算子）
# ============================================================
def test_detect_scenario_from_task_type():
    # 用有实际时间分量的规则，确保画像确认为 trend（而非被自动回推为 detail）
    frame = pd.DataFrame({
        "结束时间_月": ["2026-01", "2026-02", "2026-03"],
        "成交价": [10.0, 12.0, 11.0],
    })
    rule = {"task_type": "trend", "steps": [
        {"op": "date_part", "column": "结束时间", "parts": ["month"]},
        {"op": "aggregate", "by": ["结束时间_月"], "column": "成交价", "func": "mean"},
    ]}
    ctx = _ctx(frame, rule)
    assert ctx.profile.task_type == "trend"
    key, reasons = dt.detect_scenario(ctx)
    assert key == "trend"
    assert reasons and "画像" in reasons[0]


def test_detect_anomaly_from_clean_report():
    frame = pd.DataFrame({"v": [1, 2, 3]})
    ctx = _ctx(frame, {"steps": []},
               clean_report={"outliers": {"count": 2}})
    key, _ = dt.detect_scenario(ctx)
    assert key == "anomaly"


def test_detect_cleaning_from_ops():
    frame = pd.DataFrame({"v": [1, 2, 3]})
    ctx = _ctx(frame, {"steps": [{"op": "dedupe"}, {"op": "fill"}]})
    key, _ = dt.detect_scenario(ctx)
    assert key == "cleaning"


def test_field_features_classify_columns():
    frame = pd.DataFrame({
        "id": [1, 2, 3, 4],
        "类别": ["甲", "乙", "甲", "乙"],
        "金额": [1.0, 2.0, 3.0, 4.0],
    })
    feats = dt._field_features(frame)
    assert "金额" in feats["numeric"]
    assert "类别" in feats["categorical"]
    assert "id" in feats["high_card"]


# ============================================================
# 展示计划：目标 / 规模 / 显式配置
# ============================================================
def test_goal_guide_adds_chain_block():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "summary", "steps": [
        {"op": "aggregate", "by": ["区县"]}]})
    plan = dt.build_plan(ctx, goal="guide")
    assert any(b.kind == "chain" for b in plan.blocks)
    assert any("使用说明" in r for r in plan.reasons)


def test_large_scale_tightens_table_rows():
    frame = pd.DataFrame({"v": list(range(600))})
    ctx = _ctx(frame, {"task_type": "detail", "steps": []})
    plan = dt.build_plan(ctx)
    assert plan.scale == dt.SCALE_LARGE
    table = next(b for b in plan.blocks if b.kind == "table")
    assert table.config["max_rows"] == dt.MAX_TABLE_ROWS["large"]


def test_explicit_template_overrides_scenario():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "detail", "steps": []})
    plan = dt.build_plan(ctx, template="ranking")
    assert plan.scenario == "ranking"
    assert any("显式指定" in r for r in plan.reasons)


def test_explicit_blocks_sequence():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "detail", "steps": []})
    plan = dt.build_plan(ctx, blocks=["kpi", "table"])
    assert [b.kind for b in plan.blocks] == ["kpi", "table"]


def test_empty_charts_pruned_from_plan():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "detail", "steps": []})
    plan = dt.build_plan(ctx, template="trend")  # trend 默认含 chart 块
    assert not any(b.kind == "chart" for b in plan.blocks)


def test_unknown_template_falls_back():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "detail", "steps": []})
    plan = dt.build_plan(ctx, template="not_exists")
    assert plan.scenario == "detail"
    assert any("未知展示模板" in r for r in plan.reasons)


# ============================================================
# 块渲染与注册表
# ============================================================
def test_registered_block_renderers_cover_catalog():
    kinds = {k for spec in dt.SCENARIO_CATALOG.values() for k in spec.blocks}
    assert kinds <= set(dt.BLOCK_RENDERERS)


def test_custom_block_registration():
    frame = pd.DataFrame({"v": [1, 2]})
    ctx = _ctx(frame, {"steps": []})

    @dt.register_block("_test_custom")
    def _custom(block, ctx):
        return "<div>custom-ok</div>"

    block = dt.BlockSpec("_test_custom", "自定义")
    html = dt.render_section(block, ctx, "detail")
    assert "custom-ok" in html
    dt.BLOCK_RENDERERS.pop("_test_custom", None)


def test_table_block_truncates_rows():
    frame = pd.DataFrame({"v": list(range(100))})
    ctx = _ctx(frame, {"steps": []})
    block = dt.BlockSpec("table", "结果", config={"max_rows": 10, "max_cols": 1})
    html = dt.BLOCK_RENDERERS["table"](block, ctx)
    assert html.count("<tr>") == 11  # 1 表头 + 10 数据行
    assert "共 100 行" in html


# ============================================================
# 文档与说明
# ============================================================
def test_render_document_is_self_contained():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "summary", "steps": [
        {"op": "aggregate", "by": ["区县"]}]}, step_logs=[
        {"index": 0, "op": "aggregate", "status": "ok"}])
    plan = dt.build_plan(ctx, goal="guide")
    html = dt.render_document(
        plan, ctx, title="测试报告", kicker="K", subtitle="S", badge="B",
        meta="<span>meta</span>", footer="F",
    )
    assert "<!DOCTYPE html>" in html
    assert "data-scenario=" in html
    assert "<style>" in html and "@import" not in html
    import re
    assert not re.search(r"\{\{[A-Z_]+\}\}", html)


def test_plan_explanation_readable():
    frame = pd.DataFrame({"区县": ["A", "B"], "数量": [1, 2]})
    ctx = _ctx(frame, {"task_type": "summary", "steps": []})
    plan = dt.build_plan(ctx, goal="guide")
    text = dt.plan_explanation(plan)
    assert "场景：" in text and "展示目标：" in text and "选择理由：" in text

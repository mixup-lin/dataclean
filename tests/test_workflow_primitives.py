# -*- coding: utf-8 -*-
"""通用原语编排回归：显式公式、季度粒度、按场景动态质量报告。

严格模拟前端：POST /plan → 规则 JSON → POST /execute → GET /result。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd
import pytest

import etl_pipeline
import rules
import webapp
from ops.builtin.value_ops import apply_expr, parse_expression
from ops.intent import parse_workflow_intent
from ops.prompt_kit import primary_family
from ops.rule_eval import evaluate_rule, format_issues
from tests.provider_fixture import FixtureProvider

INTENT = (
    "去重，然后提取出面积小于50或大于1000的数据，最后按结束时间，"
    "每一年按季度分析每个季度的平均成交单价的趋势，成交单价 = （成交价/面积）"
)
INTENT_MONTH = (
    "去重，然后提取出面积小于50或大于1000的数据，最后分析2026年按月的"
    "平均成交单价的趋势，成交单价 = （成交价/面积）"
)
COLUMNS = ["标的号", "面积(㎡)", "成交价(元)", "结束时间"]


def _frame() -> pd.DataFrame:
    rows = []
    for year in (2025, 2026):
        for quarter, month in enumerate((1, 4, 7, 10), start=1):
            for area, price in ((20.0, 1000.0 + month * 10),
                                (2000.0, 3000.0 + month * 5),
                                (100.0, 2000.0 + month * 2)):
                rows.append({
                    "标的号": f"{year}-Q{quarter}-{int(area)}",
                    "面积(㎡)": area,
                    "成交价(元)": price + (year - 2025) * 1000,
                    "结束时间": f"{year}-{month:02d}-15",
                })
    return pd.DataFrame(rows)


def _month_frame() -> pd.DataFrame:
    rows = []
    for year in (2025, 2026):
        for month in range(1, 4):
            for area, price in ((20.0, 100.0 + month * 10),
                                (2000.0, 300.0 + month * 5),
                                (100.0, 200.0 + month * 2)):
                rows.append({
                    "标的号": f"{year}-{month}-{int(area)}",
                    "面积(㎡)": area,
                    "成交价(元)": price + (year - 2025) * 1000,
                    "结束时间": f"{year}-{month:02d}-15",
                })
    return pd.DataFrame(rows)


def _state(tmp_path: Path):
    return webapp.AppState(str(tmp_path / "out"))


_RULE_AREA = __import__("re").compile(
    r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>',
    __import__("re").S,
)


def _bad_rule(path: str) -> dict:
    """故意漏掉公式、季度与分析：只保留前半段清洗。"""
    return {
        "version": "2.0", "intent": INTENT,
        "source": {"path": path, "format": "csv"},
        "options": {"fill_missing": "none"}, "task_type": "detail",
        "steps": [
            {"op": "dedupe", "subset": [], "keep": "first"},
            {"op": "filter", "logic": "or", "conditions": [
                {"column": "面积(㎡)", "operator": "lt", "value": 50},
                {"column": "面积(㎡)", "operator": "gt", "value": 1000},
            ]},
        ],
        "output": {"formats": ["csv"], "report": True},
    }


def test_expr_evaluates_explicit_formula():
    frame = pd.DataFrame({"成交价(元)": [100.0, 200.0], "面积(㎡)": [10.0, 0.0]})
    step = {"op": "expr", "column": "成交单价", "expression": "c0/c1",
            "operands": ["成交价(元)", "面积(㎡)"]}
    out, _ = apply_expr(frame, step)
    assert out["成交单价"].iloc[0] == pytest.approx(10.0)
    assert pd.isna(out["成交单价"].iloc[1])  # 除零归 NaN，不产生 inf


@pytest.mark.parametrize("expression", ["__import__('os')", "c0.__class__", "(lambda: 1)"])
def test_expr_rejects_unsafe_syntax(expression):
    with pytest.raises((ValueError, SyntaxError)):
        parse_expression(expression)


def test_workflow_compiles_formula_and_quarter_without_special_casing():
    spec = parse_workflow_intent(INTENT, COLUMNS)
    assert spec is not None
    ops = [s["op"] for s in spec.steps]
    assert ops == ["dedupe", "filter", "expr", "date_part", "dropna", "filter",
                   "aggregate", "expr", "sort"]
    formula = spec.steps[2]
    assert formula["operands"] == ["成交价(元)", "面积(㎡)"]
    assert formula["expression"] == "c0/c1"
    assert spec.steps[3]["parts"] == ["year", "quarter"]
    assert spec.steps[3]["bucket"] == "年季度"
    assert spec.steps[4]["subset"] == ["结束时间"]
    assert spec.steps[5]["logic"] == "and"
    assert spec.steps[6]["by"] == ["结束时间_年季度"]
    assert spec.steps[6]["func"] == ["mean", "sum", "sum"]
    assert spec.steps[7]["operands"] == ["成交价(元)之和", "面积(㎡)之和"]
    assert spec.task_type == "trend"


def test_composite_family_does_not_force_fixed_ops():
    system, user = rules.build_rule_prompt(INTENT, {"path": "x.csv", "format": "csv"}, COLUMNS)
    payload = json.loads(user)
    assert primary_family(INTENT, COLUMNS) == "composite"
    assert payload["task_router"]["required_ops"] == []
    assert "expr" in payload["allowed_ops_for_intent"]
    assert "c0/c1" in system  # 显式公式语法进入提示词正文


def test_frontend_flow_runs_full_formula_quarter_workflow(tmp_path):
    path = tmp_path / "in.csv"
    _frame().to_csv(path, index=False, encoding="utf-8-sig")
    state = _state(tmp_path)

    original = rules.get_provider
    rules.get_provider = lambda: FixtureProvider(rule=_bad_rule(str(path)))
    try:
        reply = webapp.handle_request("POST", "/plan", {}, {
            "intent": [INTENT], "source_path": [str(path)], "source_format": ["csv"],
            "columns": [",".join(COLUMNS)], "formats": ["csv"], "report": ["1"],
        }, state)
    finally:
        rules.get_provider = original
    assert reply.status == 200, reply.body[:800]
    match = _RULE_AREA.search(reply.body)
    assert match
    rule = json.loads(html.unescape(match.group(1)))

    ops = [s["op"] for s in rule["steps"]]
    assert ops == ["dedupe", "filter", "expr", "date_part", "dropna", "filter",
                   "aggregate", "expr", "sort"], ops
    assert rule["steps"][2]["column"] == "成交单价"
    assert rule["steps"][3]["parts"] == ["year", "quarter"]
    assert rule["steps"][3]["bucket"] == "年季度"
    assert rule["steps"][0]["subset"] == ["标的号"]
    assert rule["task_type"] == "trend"

    execute = webapp.handle_request(
        "POST", "/execute", {},
        {"rule_json": [json.dumps(rule, ensure_ascii=False)]}, state,
    )
    assert execute.status == 302, execute.body[:800]
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    assert summary["task"]["task_type"] == "trend"
    assert all(s["status"] == "ok" for s in summary["step_logs"]), summary["step_logs"]
    result = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    assert "成交单价" in result.columns
    assert "结束时间_年季度" in result.columns
    assert result["结束时间_年季度"].nunique() >= 6, result
    assert result["结束时间_年季度"].isna().sum() == 0, result
    # 总价/总面积口径：与逐条均值不同，且每个时间桶都带年份
    assert not result["成交单价"].equals(result["成交单价·逐条均值"]), result
    assert all(str(v).startswith(("2025Q", "2026Q")) for v in result["结束时间_年季度"]), result

    report = (run_dir / summary["task_report_name"]).read_text(encoding="utf-8")
    assert "时间趋势" in report
    assert "数值列相关性热力图" not in report
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    assert page.status == 200


def test_quality_report_changes_with_scenario(tmp_path):
    frame = pd.DataFrame({
        "结束时间": ["2026-01-05", "2026-02-05", "2026-03-05", "2026-04-05"],
        "面积(㎡)": [10.0, 20.0, 30.0, 40.0],
        "成交价(元)": [100.0, 200.0, 300.0, 400.0],
    })
    common = dict(output_dir=str(tmp_path), input_path="in.csv", input_format="csv")
    trend_cfg = etl_pipeline.PipelineConfig(
        **common, time_column="结束时间", resample_freq="ME"
    )
    general_cfg = etl_pipeline.PipelineConfig(**common)

    summary_table = frame.describe()
    trend_html = Path(etl_pipeline.generate_report(
        frame, {}, {"summary": summary_table, "resampled": pd.DataFrame({
            "结束时间": ["2026-01", "2026-02", "2026-03"],
            "成交价(元)": [100.0, 200.0, 300.0],
        })}, trend_cfg,
    )).read_text(encoding="utf-8")
    general_html = Path(etl_pipeline.generate_report(
        frame, {}, {"summary": summary_table}, general_cfg,
    )).read_text(encoding="utf-8")

    assert "时间趋势" in trend_html and "周期覆盖度" in trend_html
    assert "数值列相关性热力图" not in trend_html
    assert "周期覆盖度" in trend_html
    assert "数据分布图表" in general_html


def test_workflow_compiles_explicit_year_filter_for_monthly_ratio():
    spec = parse_workflow_intent(INTENT_MONTH, COLUMNS)
    assert spec is not None
    ops = [step["op"] for step in spec.steps]
    assert ops == [
        "dedupe", "filter", "expr", "date_filter", "date_part", "dropna",
        "filter", "aggregate", "expr", "sort",
    ], ops
    assert spec.steps[3] == {
        "op": "date_filter", "column": "结束时间", "year": 2026,
    }
    assert ops.index("date_filter") < ops.index("date_part")
    assert spec.steps[4]["parts"] == ["year", "month"]
    assert spec.steps[4]["bucket"] == "年月"
    assert spec.steps[7]["by"] == ["结束时间_年月"]
    assert spec.steps[8]["operands"] == ["成交价(元)之和", "面积(㎡)之和"]
    assert spec.task_type == "trend"
    assert not evaluate_rule(
        {"task_type": spec.task_type, "steps": spec.steps}, INTENT_MONTH, COLUMNS
    )


def test_eval_catches_missing_explicit_year_filter_in_trend():
    spec = parse_workflow_intent(INTENT_MONTH, COLUMNS)
    steps = [step for step in spec.steps if step["op"] != "date_filter"]
    issues = evaluate_rule(
        {"task_type": spec.task_type, "steps": steps}, INTENT_MONTH, COLUMNS
    )
    assert any("missing_date_filter" in issue for issue in format_issues(issues))


def test_prompt_aligns_explicit_year_and_ratio_aliases():
    system, user = rules.build_rule_prompt(
        INTENT_MONTH, {"path": "x.csv", "format": "csv"}, COLUMNS
    )
    payload = json.loads(user)
    hints = payload["task_router"]["extracted_hints"]
    assert hints["date_hint"] == {"year": 2026}
    assert hints["forced_date_step"] == {
        "op": "date_filter", "column": "结束时间", "year": 2026,
    }
    assert "成交价(元)之和" in system
    assert "面积(㎡)之和" in system
    assert "date_part 之前" in system


def test_frontend_flow_filters_2026_for_monthly_ratio(tmp_path):
    path = tmp_path / "in.csv"
    _month_frame().to_csv(path, index=False, encoding="utf-8-sig")
    state = _state(tmp_path)

    original = rules.get_provider
    rules.get_provider = lambda: FixtureProvider(rule=_bad_rule(str(path)))
    try:
        reply = webapp.handle_request("POST", "/plan", {}, {
            "intent": [INTENT_MONTH], "source_path": [str(path)], "source_format": ["csv"],
            "columns": [",".join(COLUMNS)], "formats": ["csv"], "report": ["1"],
        }, state)
    finally:
        rules.get_provider = original
    assert reply.status == 200, reply.body[:800]
    match = _RULE_AREA.search(reply.body)
    assert match
    rule = json.loads(html.unescape(match.group(1)))

    ops = [step["op"] for step in rule["steps"]]
    assert ops == [
        "dedupe", "filter", "expr", "date_filter", "date_part", "dropna",
        "filter", "aggregate", "expr", "sort",
    ], ops
    assert rule["steps"][3] == {
        "op": "date_filter", "column": "结束时间", "year": 2026,
    }
    assert rule["task_type"] == "trend"
    assert not evaluate_rule(rule, INTENT_MONTH, COLUMNS)

    execute = webapp.handle_request(
        "POST", "/execute", {},
        {"rule_json": [json.dumps(rule, ensure_ascii=False)]}, state,
    )
    assert execute.status == 302, execute.body[:800]
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    assert all(step["status"] == "ok" for step in summary["step_logs"]), summary["step_logs"]
    result = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    assert list(result["结束时间_年月"]) == ["2026-01", "2026-02", "2026-03"]
    assert not result["结束时间_年月"].astype(str).str.startswith("2025").any()
    assert result["成交单价"].iloc[0] == pytest.approx(2415.0 / 2020.0)
    assert result["成交单价"].iloc[0] != pytest.approx(result["成交单价·逐条均值"].iloc[0])

    report = (run_dir / summary["task_report_name"]).read_text(encoding="utf-8")
    assert "时间趋势" in report
    assert "数值列相关性热力图" not in report
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    assert page.status == 200


def test_correct_composite_llm_rule_is_not_replaced(tmp_path):
    """编译器覆盖不到的业务原语不应因为确定性替换而丢失。"""
    path = tmp_path / "in.csv"
    _month_frame().to_csv(path, index=False, encoding="utf-8-sig")
    spec = parse_workflow_intent(INTENT_MONTH, COLUMNS)
    llm_rule = {
        "version": "2.0",
        "intent": INTENT_MONTH,
        "source": {"path": str(path), "format": "csv"},
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": spec.task_type,
        "steps": [dict(step) for step in spec.steps],
        "output": {"formats": ["csv"], "report": True},
    }
    rule, meta = rules.generate_rules(
        INTENT_MONTH, {"path": str(path), "format": "csv"}, COLUMNS,
        provider=FixtureProvider(rule=llm_rule),
    )
    assert meta.get("semantic_issues") == [], meta
    assert meta["intent_plan"]["decision"] == "preserved_correct_llm"
    assert meta["intent_plan"]["applied"] is False
    assert [step["op"] for step in rule["steps"]] == [step["op"] for step in spec.steps]

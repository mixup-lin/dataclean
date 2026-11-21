"""新增算子与任务画像的场景测试（不依赖网络与 API Key）。

覆盖：占比三口径、透视、日期聚合、分箱、条件赋值、字符串/类型、TopN、
任务类型识别、任务报告与多视图导出。规则一律显式给出，不再依赖 Mock 模板。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from rule_executor import apply_step, execute_rule
from tests.provider_fixture import make_rule

ROOT = Path(__file__).resolve().parents[1]
LOTS = ROOT / "lots.csv"

SAMPLE_CSV = """小区,户型,成交价(元),成交日期,状态,备注
A,三居,100,2024-01-05,成交, 好房 
A,三居,200,2024-02-11,成交,一般
A,两居,300,2024-02-20,成交,好房
B,三居,400,2024-03-02,成交,一般
B,两居,,2024-03-15,流拍,好房
C,两居,500,2025-01-09,成交,好房
C,三居,600,2025-02-01,成交,一般
"""


def _sample() -> pd.DataFrame:
    from io import StringIO

    return pd.read_csv(StringIO(SAMPLE_CSV))


# ============================================================
# 占比三口径
# ============================================================
def test_pct_condition_scope():
    out, note = apply_step(_sample(), {
        "op": "pct", "scope": "condition",
        "condition": {"column": "成交价(元)", "operator": "gt", "value": 0},
        "alias": "成交占比",
    })
    assert len(out) == 1
    assert out.loc[0, "分子"] == 6
    assert out.loc[0, "分母"] == 7
    assert out.loc[0, "成交占比"] == pytest.approx(85.7143)
    assert "6/7" in note


def test_pct_overall_shares_and_cumulative():
    df = _sample()
    agg, _ = apply_step(df, {
        "op": "aggregate", "by": "小区", "column": "成交价(元)",
        "func": "count", "alias": "条数",
    })
    pct, _ = apply_step(agg, {
        "op": "pct", "scope": "overall", "by": "小区", "column": "条数",
        "alias": "占比",
    })
    assert pct["占比"].sum() == pytest.approx(100.0, abs=0.01)
    assert pct["分母"].nunique() == 1 and pct.loc[0, "分母"] == 7

    cumulative, _ = apply_step(agg, {
        "op": "pct", "scope": "overall", "by": "小区", "column": "条数",
        "alias": "占比", "cumulative": True,
    })
    assert cumulative["占比"].iloc[-1] == pytest.approx(100.0, abs=0.01)
    assert cumulative["占比"].is_monotonic_increasing


def test_pct_group_scope_normalises_within_group():
    df = _sample()
    agg, _ = apply_step(df, {
        "op": "aggregate", "by": ["小区", "户型"], "column": "成交价(元)",
        "func": "count", "alias": "条数",
    })
    pct, _ = apply_step(agg, {
        "op": "pct", "scope": "group", "by": "小区", "within": "户型",
        "column": "条数", "alias": "组内占比",
    })
    per_group = pct.groupby("小区")["组内占比"].sum()
    assert all(v == pytest.approx(100.0, abs=0.01) for v in per_group)
    assert (pct["组内占比"] <= 100).all()


# ============================================================
# 透视 / 日期 / 分箱
# ============================================================
def test_pivot_cross_table():
    out, note = apply_step(_sample(), {
        "op": "pivot", "index": "小区", "columns": "户型",
        "values": "成交价(元)", "aggfunc": "count",
    })
    assert "小区" in out.columns
    assert len(out) == 3
    assert "两居" in out.columns and "三居" in out.columns
    assert "透视表" in note


def test_date_part_then_aggregate():
    parsed, _ = apply_step(_sample(), {
        "op": "date_part", "column": "成交日期", "parts": ["year", "month"],
    })
    assert "成交日期_年" in parsed.columns and "成交日期_月" in parsed.columns
    agg, _ = apply_step(parsed, {
        "op": "aggregate", "by": "成交日期_月", "column": "成交价(元)",
        "func": "count", "alias": "条数",
    })
    assert set(int(v) for v in agg["成交日期_月"]) == {1, 2, 3}
    assert agg["条数"].sum() == 7


def test_bin_distribution_is_ordered_categorical():
    out, _ = apply_step(_sample(), {
        "op": "bin", "column": "成交价(元)", "bins": 3, "method": "width",
        "into": "价格区间",
    })
    assert "价格区间" in out.columns
    assert isinstance(out["价格区间"].dtype, pd.CategoricalDtype)
    counts = out["价格区间"].value_counts(dropna=False)
    assert counts.sum() == 7


# ============================================================
# 字符串 / 类型 / 赋值 / 排名 / 去重 / 填充
# ============================================================
def test_string_cast_split_concat_chain():
    out, _ = apply_step(_sample(), {"op": "string", "column": "备注", "action": "strip"})
    assert out["备注"].iloc[0] == "好房"

    out, _ = apply_step(out, {"op": "split", "column": "成交日期", "sep": "-",
                              "into": ["年", "月", "日"]})
    assert out["年"].iloc[0] == "2024"

    out, _ = apply_step(out, {"op": "cast", "columns": ["年"], "to": "int"})
    assert int(out["年"].iloc[0]) == 2024

    out, _ = apply_step(out, {"op": "concat", "columns": ["小区", "户型"],
                              "into": "标签", "sep": "/"})
    assert out["标签"].iloc[0] == "A/三居"


def test_assign_bin_and_rank_topn():
    out, _ = apply_step(_sample(), {
        "op": "assign", "column": "价位", "default": "低",
        "rules": [
            {"condition": {"column": "成交价(元)", "operator": "gte", "value": 500},
             "value": "高"},
            {"condition": {"column": "成交价(元)", "operator": "gte", "value": 300},
             "value": "中"},
        ],
    })
    assert out["价位"].tolist()[:4] == ["低", "低", "中", "中"]

    ranked, note = apply_step(_sample(), {
        "op": "rank", "by": "成交价(元)", "into": "排名",
        "method": "dense", "group_by": "小区", "topn": 1,
    })
    assert len(ranked) == 3
    assert ranked["排名"].max() == 1
    assert "Top1" in note


def test_dedupe_and_fill():
    deduped, note = apply_step(_sample(), {"op": "dedupe", "subset": ["小区"]})
    assert len(deduped) == 3 and "去重 4 行" in note

    filled, _ = apply_step(_sample(), {"op": "fill", "column": "成交价(元)",
                                       "method": "median"})
    assert filled["成交价(元)"].isna().sum() == 0


# ============================================================
# 多条件筛选
# ============================================================
def test_multi_condition_filter_and_or_in_between():
    both, _ = apply_step(_sample(), {
        "op": "filter", "logic": "and",
        "conditions": [
            {"column": "状态", "operator": "eq", "value": "成交"},
            {"column": "成交价(元)", "operator": "between", "value": [200, 500]},
        ],
    })
    assert len(both) == 4

    either, _ = apply_step(_sample(), {
        "op": "filter", "logic": "or",
        "conditions": [
            {"column": "小区", "operator": "in", "value": ["A", "B"]},
            {"column": "状态", "operator": "contains", "value": "流"},
        ],
    })
    assert len(either) == 5

    not_null, _ = apply_step(_sample(), {
        "op": "filter", "column": "成交价(元)", "operator": "notna",
    })
    assert len(not_null) == 6


# ============================================================
# 任务画像与报告
# ============================================================
def test_detect_task_type_matrix():
    from task_profile import detect_task_type

    cases = [
        ({"steps": [{"op": "join"}]}, "join"),
        ({"steps": [{"op": "pct"}]}, "proportion"),
        ({"steps": [{"op": "pivot"}]}, "pivot"),
        # date_part 只是筛选/派生中间步骤，不再单独构成趋势任务
        ({"steps": [{"op": "date_part"}]}, "detail"),
        ({"steps": [{"op": "bin"}]}, "distribution"),
        ({"steps": [{"op": "rank"}]}, "ranking"),
        ({"steps": [{"op": "aggregate"}, {"op": "sort"}]}, "ranking"),
        ({"steps": [{"op": "aggregate"}]}, "summary"),
        ({"steps": [{"op": "select"}]}, "detail"),
    ]
    for rule, expected in cases:
        assert detect_task_type(rule) == expected


def test_explicit_rules_cover_task_scenarios():
    """七类常见分析意图都有显式规则表达，且能通过注册表契约校验。"""
    from ops.prompt import build_catalog
    from rules import assert_valid_rule, probe_columns

    cols = probe_columns({"path": str(LOTS), "format": "csv"})
    scenarios = [
        [{"op": "pct", "scope": "condition",
          "condition": {"column": "成交价(元)", "operator": "gt", "value": 0}, "alias": "成交占比"}],
        [{"op": "aggregate", "by": "小区", "column": "成交价(元)", "func": "count", "alias": "条数"},
         {"op": "pct", "scope": "overall", "by": "小区", "column": "条数", "alias": "占比", "cumulative": True}],
        [{"op": "aggregate", "by": ["小区", "物业类型"], "column": "成交价(元)", "func": "count", "alias": "条数"},
         {"op": "pct", "scope": "group", "by": "小区", "within": "物业类型", "column": "条数", "alias": "组内占比"}],
        [{"op": "rank", "by": "成交价(元)", "into": "排名", "topn": 10}],
        [{"op": "date_part", "column": "结束时间", "parts": ["year"]},
         {"op": "count_rows", "by": "结束时间_年", "alias": "条数"}],
        [{"op": "bin", "column": "成交价(元)", "bins": 5, "into": "价格区间"},
         {"op": "count_rows", "by": "价格区间", "alias": "条数"}],
        [{"op": "resample", "column": "结束时间", "freq": "M", "funcs": ["count"]}],
    ]
    catalog = build_catalog()
    for steps in scenarios:
        rule = make_rule(source_path=str(LOTS), steps=steps)
        assert_valid_rule(rule, cols)
        for step in steps:
            assert f"- {step['op']}:" in catalog


def test_execute_proportion_task_writes_task_report(tmp_path):
    rule = make_rule(
        source_path=str(LOTS),
        steps=[
            {"op": "filter", "column": "成交价(元)", "operator": "gt", "value": 0},
            {"op": "aggregate", "by": "小区", "column": "成交价(元)",
             "func": "count", "alias": "条数"},
            {"op": "pct", "scope": "overall", "by": "小区",
             "column": "条数", "alias": "占比", "cumulative": True},
        ],
        output={"formats": ["csv", "xlsx"], "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))

    task = summary["task"]
    assert task["task_type"] == "proportion"
    assert 1 <= len(task["kpis"]) <= 6
    assert 1 <= len(task["charts"]) <= 3
    assert "占" in task["conclusion"]

    run_dir = tmp_path / summary["run_id"]
    assert summary["task_report_name"]
    report = run_dir / summary["task_report_name"]
    assert report.exists()
    html = report.read_text(encoding="utf-8")
    assert "kpi-band" in html and "chart-svg" in html
    assert len(html.encode("utf-8")) < 400_000

    names = [a["name"] for a in summary["artifacts"]]
    assert any(n.startswith("task_report_") for n in names)
    assert any(n.startswith("quality_report_") for n in names)
    # 附表：小区分组汇总
    assert any(n.startswith("table_") for n in names)
    assert (run_dir / "table_1.csv").exists()

    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.load_workbook(run_dir / "result.xlsx", read_only=True)
    assert len(workbook.sheetnames) >= 2
    workbook.close()


def test_execute_multi_condition_detail_task(tmp_path):
    rule = make_rule(
        source_path=str(LOTS),
        steps=[
            {"op": "filter", "column": "起拍价(元)", "operator": "gt", "value": 0},
            {"op": "sort", "by": "成交价(元)", "ascending": False},
            {"op": "head", "n": 5},
        ],
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["rows"] <= 5
    assert summary["task"]["task_type"] in ("detail", "ranking")
    json.dumps(summary["task"], ensure_ascii=False)


def test_rule_json_with_sources_roundtrip(tmp_path):
    """带 sources 的规则可 JSON 序列化并再次校验。"""
    from rules import assert_valid_rule, probe_columns

    dim = tmp_path / "dim.csv"
    dim.write_text("小区名称,片区\nA,城东\nB,城西\n", encoding="utf-8-sig")
    rule = {
        "version": "2.0",
        "intent": "按小区关联维表",
        "sources": {
            "main": {"path": str(LOTS), "format": "csv"},
            "dim": {"path": str(dim), "format": "csv"},
        },
        "options": {},
        "steps": [{
            "op": "join", "right": "dim", "how": "left",
            "left_on": "小区", "right_on": "小区名称",
        }],
        "output": {"formats": ["csv"], "report": False},
    }
    cols = probe_columns(rule["sources"]["main"]) + probe_columns(rule["sources"]["dim"])
    assert_valid_rule(rule, list(dict.fromkeys(cols)))
    summary = execute_rule(json.loads(json.dumps(rule)), base_output=str(tmp_path))
    assert "片区" in summary["rule"]["steps"][0].get("right", "未执行") or summary["rows"] > 0
    assert summary["task"]["join"][0]["how"] == "left"
    assert "片区" in pd.read_csv(summary["result_csv"]).columns

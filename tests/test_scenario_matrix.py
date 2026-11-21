# -*- coding: utf-8 -*-
"""场景矩阵：比率意图 × LLM 规则形态 × 编译兜底 × 非比率防误伤 × 端到端。

目标：验证「换一种问法 / 换一种 LLM 输出」结果不变。矩阵分五层：
1. 意图解析：完整描述、简写、承前省略、非比率描述；
2. 规则生成：5 种实测错误规则形态统一替换为确定性骨架；
3. 编译兜底：绕过生成器的规则（手填 / 规则库旧规则）归一且幂等；
4. 非比率防误伤：计数 / 占比 / 日期 / join / pivot 等链路原样保留；
5. 端到端：合成数据全形态收敛 + 真实数据成交率 / 未成交率。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import rule_executor as rx
import rules
from ops.compiler import compile_rule, normalize_rate_pipeline
from ops.intent import apply_intent_plan, parse_rate_intent, target_from_intent
from tests.provider_fixture import FixtureProvider, make_rule

COLS = ["市", "区县", "小区", "成交价(元)", "结束时间", "物业类型"]

INTENT_MISS = (
    "筛选：市：福州 \n判断：成交价为空-未成交，成交价不为空-成交\n"
    "目标：统计未成交率（未成交/总数）"
)
INTENT_HIT = (
    "描述：“筛选：区县：鼓楼区 判断：成交价为空-未成交，成交价不为空-成交"
    "目标：统计成交率（成交/未成交）”"
)
INTENT_COLLOQUIAL = "统计市是福州的数据，成交价为空算未成交，不为空算成交，求成交率"
INTENT_EMPTY_RATE = "统计成交价为空率"
INTENT_NOTNA_PCT = "筛选：区县：鼓楼区，统计成交价不为空占比"
INTENT_GROUP_MISS = (
    "筛选：市：福州；判断：成交价为空-未成交，成交价不为空-成交；"
    "目标：按区县分组统计未成交率"
)
INTENT_SHORT_MISS = "筛选市：福州，统计未成交率"
INTENT_SHORT_HIT = "筛选市：福州，统计成交率"
INTENT_COUNT = "筛选市：福州，按小区分组，统计每个小区有几条数据"

# 实测 LLM 对同一描述生成的 5 种规则形态（4 种错误）
BAD_RULE_FORMS = {
    "contradictory_filters": [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "成交价(元)", "operator": "isna", "value": True}], "logic": "and"},
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "成交价(元)", "operator": "notna", "value": True}], "logic": "and"},
    ],
    "filter_then_rate_then_pct": [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "成交价(元)", "operator": "isna", "value": True}], "logic": "and"},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交", "alias": "成交状态"},
        {"op": "pct", "scope": "condition",
         "condition": {"column": "成交状态", "operator": "eq", "value": "未成交"},
         "alias": "未成交率"},
    ],
    "assign_plus_count": [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"}], "logic": "and"},
        {"op": "assign", "column": "状态", "rules": [
            {"condition": {"column": "成交价(元)", "operator": "isna"}, "value": "未成交"},
            {"condition": {"column": "成交价(元)", "operator": "notna"}, "value": "成交"}],
         "default": "未知"},
        {"op": "count_rows", "by": ["状态"], "alias": "条数"},
    ],
    "pct_only": [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"}], "logic": "and"},
        {"op": "pct", "scope": "condition",
         "condition": {"column": "成交价(元)", "operator": "isna"},
         "alias": "未成交率"},
    ],
    "wrong_direction_rate": [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"}], "logic": "and"},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交", "alias": "成交率"},
    ],
}


def _clone(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


@pytest.fixture
def sample_csv(tmp_path):
    """福州 6 行（成交 3 / 未成交 3），厦门 2 行；鼓楼区成交 2 / 未成交 1。"""
    frame = pd.DataFrame({
        "市": ["福州"] * 6 + ["厦门"] * 2,
        "区县": ["鼓楼区"] * 3 + ["台江区"] * 3 + ["思明区"] * 2,
        "小区": ["A", "A", "B", "B", "C", "C", "D", "D"],
        "成交价(元)": [100.0, None, 200.0, None, 300.0, None, 400.0, 500.0],
        "结束时间": [
            "2026-03-08T10:00:00+08:00", "2026-07-01", "2025-01-01",
            "2026-12-31", "2026-05-05", "2025-08-08", "2026-01-01", "2026-02-02",
        ],
    })
    target = tmp_path / "matrix.csv"
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    return str(target)


# ------------------------------------------------------------
# 1. 意图解析矩阵
# ------------------------------------------------------------
@pytest.mark.parametrize("text, alias, col, operator, num, filters, by", [
    (INTENT_MISS, "未成交率", "成交价(元)", "notna", "miss",
     [("市", "eq", "福州")], []),
    (INTENT_HIT, "成交率", "成交价(元)", "notna", "hit",
     [("区县", "eq", "鼓楼区")], []),
    (INTENT_COLLOQUIAL, "成交率", "成交价(元)", "notna", "hit",
     [("市", "eq", "福州")], []),
    (INTENT_EMPTY_RATE, "成交价为空率", "成交价(元)", "isna", "hit", [], []),
    (INTENT_NOTNA_PCT, "成交价不为空占比", "成交价(元)", "notna", "hit",
     [("区县", "eq", "鼓楼区")], []),
    (INTENT_GROUP_MISS, "未成交率", "成交价(元)", "notna", "miss",
     [("市", "eq", "福州")], ["区县"]),
])
def test_intent_matrix(text, alias, col, operator, num, filters, by):
    spec = parse_rate_intent(text, COLS)
    assert spec is not None, f"高置信度意图不应丢弃：{text}"
    assert spec.alias == alias
    assert spec.judge_column == col
    assert spec.condition["operator"] == operator
    assert spec.numerator == num
    assert [(f["column"], f["operator"], f.get("value")) for f in spec.filters] == filters
    assert spec.by == by


@pytest.mark.parametrize("text", [INTENT_COUNT, "筛选市：福州，按小区分组，统计条数"])
def test_non_rate_intent_not_parsed_as_rate(text):
    assert parse_rate_intent(text, COLS) is None


@pytest.mark.parametrize("text, label", [
    (INTENT_SHORT_MISS, "未成交"), (INTENT_SHORT_HIT, "成交"),
])
def test_short_intent_still_exposes_target_direction(text, label):
    target = target_from_intent(text)
    assert target is not None and target["target_label"] == label


# ------------------------------------------------------------
# 2. 规则生成层：LLM 输出只是可替换输入
# ------------------------------------------------------------
@pytest.mark.parametrize("name", list(BAD_RULE_FORMS))
def test_apply_intent_plan_replaces_llm_chain(name):
    rule = make_rule(intent=INTENT_MISS, source_path="lots.csv",
                     steps=_clone(BAD_RULE_FORMS[name]))
    planned, warnings, info = apply_intent_plan(rule, INTENT_MISS, COLS)
    assert info["applied"] is True
    ops = [s["op"] for s in planned["steps"]]
    assert ops == ["filter", "rate"], (name, ops)
    assert planned["steps"][-1]["alias"] == "未成交率"
    assert planned["steps"][-1]["numerator"] == "miss"
    assert planned["task_type"] == "proportion"
    assert rules.validate_rule(planned, COLS) == []
    assert any("意图编译" in w for w in warnings)


def test_apply_intent_plan_keeps_non_rate_rule_untouched():
    steps = [{"op": "count_rows", "by": ["小区"], "alias": "条数"}]
    rule = make_rule(intent=INTENT_COUNT, source_path="lots.csv", steps=steps)
    planned, warnings, info = apply_intent_plan(rule, INTENT_COUNT, COLS)
    assert info == {} and warnings == [] and planned["steps"] == steps


# ------------------------------------------------------------
# 3. 编译层：手填 / 旧规则兜底，幂等
# ------------------------------------------------------------
@pytest.mark.parametrize("name", list(BAD_RULE_FORMS))
def test_compile_fallback_converges_and_is_idempotent(name):
    rule = {"version": "2.0", "intent": INTENT_MISS, "steps": _clone(BAD_RULE_FORMS[name])}
    plan = compile_rule(rule, COLS)
    rate = [s for s in plan.steps if s["op"] == "rate"]
    assert len(rate) == 1, name
    assert rate[0]["alias"] == "未成交率"
    assert rate[0]["numerator"] == "miss"
    assert plan.warnings, name
    again = compile_rule({"version": "2.0", "intent": INTENT_MISS,
                          "steps": _clone(plan.steps)}, COLS)
    assert again.steps == plan.steps, name


@pytest.mark.parametrize("text, alias, num", [
    (INTENT_SHORT_MISS, "未成交率", "miss"),
    (INTENT_SHORT_HIT, "成交率", "hit"),
])
def test_short_intent_fixes_direction_without_guessing_column(text, alias, num):
    steps = [{"op": "filter", "conditions": [
        {"column": "市", "operator": "eq", "value": "福州"}], "logic": "and"},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交", "alias": "成交率"}]
    plan = compile_rule({"version": "2.0", "intent": text, "steps": steps}, COLS)
    rate = [s for s in plan.steps if s["op"] == "rate"][0]
    assert rate["alias"] == alias and rate.get("numerator", "hit") == num


@pytest.mark.parametrize("intent, steps", [
    (INTENT_COUNT, [
        {"op": "filter", "conditions": [
            {"column": "小区", "operator": "notna"}], "logic": "and"},
        {"op": "count_rows", "by": ["小区"], "alias": "条数"},
    ]),
    ("统计区县是鼓楼区的占比", [
        {"op": "pct", "scope": "condition",
         "condition": {"column": "区县", "operator": "eq", "value": "鼓楼区"},
         "alias": "鼓楼区占比"},
    ]),
    ("按结束时间统计每月条数", [
        {"op": "date_filter", "column": "结束时间", "year": 2026},
        {"op": "resample", "column": "结束时间", "freq": "M", "agg": "count"},
    ]),
    ("统计各区县成交额并排序", [
        {"op": "aggregate", "by": ["区县"], "column": "成交价(元)", "func": "sum",
         "alias": "成交额"},
        {"op": "sort", "by": ["成交额"], "ascending": [False]},
    ]),
])
def test_non_rate_pipelines_are_not_rewritten(intent, steps):
    plan = compile_rule({"version": "2.0", "intent": intent, "steps": _clone(steps)}, COLS)
    assert [s["op"] for s in plan.steps] == [s["op"] for s in steps]
    assert not any("rate" in w for w in plan.warnings)


def test_nested_conditions_collect_all_columns():
    steps = [{"op": "filter", "conditions": [
        {"column": "市", "operator": "eq", "value": "福州"},
        {"logic": "or", "conditions": [
            {"column": "成交价(元)", "operator": "notna"},
            {"column": "区县", "operator": "eq", "value": "鼓楼区"}]}]}]
    assert rx._filter_columns({"steps": steps}) == {"市", "成交价(元)", "区县"}


# ------------------------------------------------------------
# 4. 端到端：合成数据，全形态收敛
# ------------------------------------------------------------
@pytest.mark.parametrize("name", list(BAD_RULE_FORMS))
def test_all_bad_forms_produce_same_rate(sample_csv, tmp_path, name):
    rule = make_rule(
        intent=INTENT_MISS, source_path=sample_csv,
        steps=_clone(BAD_RULE_FORMS[name]),
        options={"fill_missing": "median", "outlier_method": "none"},
    )
    res = rx.execute_rule(rule, base_output=str(tmp_path / ("e2e_" + name)))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    assert res["task"]["task_type"] == "proportion"
    assert res["rows"] == 1
    assert (int(frame["成交数"].iloc[0]), int(frame["未成交数"].iloc[0])) == (3, 3)
    assert frame["未成交率"].iloc[0] == 50.0
    assert "未成交率 50.00%" in res["task"]["conclusion"]
    assert [s["status"] for s in res["step_logs"]] == ["ok", "ok"]
    assert res["safety_warnings"] == []


def test_full_generate_then_execute_chain_with_broken_llm_output(sample_csv, tmp_path):
    """用户实际路径：界面输入描述 → LLM 规则 → 意图编译 → 执行。

    Provider 故意返回最糟糕的「两个互相矛盾的 filter」，验证结果完全由意图骨架决定。
    """
    from tests.provider_fixture import FixtureProvider

    payload = {
        "intent": INTENT_MISS,
        "source": {"path": sample_csv, "format": "csv"},
        "columns": COLS,
    }
    provider = FixtureProvider(rule={
        "version": "2.0", "task_type": "summary",
        "steps": _clone(BAD_RULE_FORMS["contradictory_filters"]),
        "options": {"fill_missing": "median"},
    })
    rule, meta = rules.generate_rules_from_dict(payload, provider=provider)
    assert meta["intent_plan"]["applied"] is True
    assert any("意图编译" in w for w in meta.get("reconcile_warnings", []))
    rule["output"] = {"formats": ["csv"], "report": False}
    res = rx.execute_rule(rule, base_output=str(tmp_path / "full_chain"))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    assert int(frame["总数"].iloc[0]) == 6
    assert (int(frame["成交数"].iloc[0]), int(frame["未成交数"].iloc[0])) == (3, 3)
    assert frame["未成交率"].iloc[0] == 50.0
    assert res["task"]["task_type"] == "proportion"


def test_generated_skeleton_runs_end_to_end(sample_csv, tmp_path):
    base = make_rule(intent=INTENT_HIT, source_path=sample_csv, steps=[])
    planned, _, info = apply_intent_plan(base, INTENT_HIT, COLS)
    assert info["applied"]
    planned["output"] = {"formats": ["csv"], "report": False}
    res = rx.execute_rule(planned, base_output=str(tmp_path / "generated"))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    assert (int(frame["成交数"].iloc[0]), int(frame["未成交数"].iloc[0])) == (2, 1)
    assert frame["成交率"].iloc[0] == 66.6667


def test_grouped_rate_end_to_end(sample_csv, tmp_path):
    rule = make_rule(
        intent=INTENT_GROUP_MISS, source_path=sample_csv,
        steps=_clone(BAD_RULE_FORMS["assign_plus_count"]),
        options={"fill_missing": "median", "outlier_method": "none"},
    )
    res = rx.execute_rule(rule, base_output=str(tmp_path / "grouped"))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    assert set(frame["区县"]) == {"鼓楼区", "台江区"}
    gulou = frame[frame["区县"] == "鼓楼区"].iloc[0]
    assert (int(gulou["成交数"]), int(gulou["未成交数"])) == (2, 1)
    assert gulou["未成交率"] == 33.3333


def test_date_and_notna_filters_survive_rate_normalization(sample_csv, tmp_path):
    steps = [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "小区", "operator": "notna"}], "logic": "and"},
        {"op": "date_filter", "column": "结束时间", "year": 2026},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交", "alias": "未成交率",
         "numerator": "miss"},
    ]
    rule = make_rule(intent=INTENT_MISS, source_path=sample_csv, steps=steps,
                     options={"fill_missing": "median", "outlier_method": "none"})
    res = rx.execute_rule(rule, base_output=str(tmp_path / "dated"))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    # 福州 6 行中 2026 年 4 行（含 1 行带时区、1 行 12-31）
    assert int(frame["总数"].iloc[0]) == 4
    assert any("小区" in json.dumps(s, ensure_ascii=False) for s in res["rule"]["steps"])


# ------------------------------------------------------------
# 5. 端到端：真实数据，两个方向互为正反例
# ------------------------------------------------------------
@pytest.fixture(scope="module")
def real_csv():
    path = Path("lots.csv")
    if not path.exists():
        pytest.skip("lots.csv 不存在，跳过真实数据矩阵")
    return str(path.resolve())


@pytest.mark.parametrize("intent, steps, expected", [
    (INTENT_MISS, BAD_RULE_FORMS["contradictory_filters"],
     ("未成交率", 7702, 12252, 19954, 61.4012)),
    (INTENT_HIT, BAD_RULE_FORMS["assign_plus_count"],
     ("成交率", 1116, 1472, 2588, 43.1221)),
])
def test_real_data_rate_matrix(real_csv, tmp_path, intent, steps, expected):
    # 每条用例用自身描述里的筛选条件，替换掉形态样例里的固定条件
    alias, hit, miss, total, rate = expected
    work = _clone(steps)
    filter_col, filter_val = ("市", "福州") if alias == "未成交率" else ("区县", "鼓楼区")
    for step in work:
        if step.get("op") == "filter" and "conditions" in step:
            step["conditions"] = [
                {"column": filter_col, "operator": "eq", "value": filter_val}
            ]
    rule = make_rule(intent=intent, source_path=real_csv, steps=work,
                     options={"fill_missing": "median", "outlier_method": "none",
                              "drop_duplicates": True})
    rule["output"] = {"formats": ["csv"], "report": False}
    res = rx.execute_rule(rule, base_output=str(tmp_path / ("real_" + alias)))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    row = frame.iloc[0]
    assert (int(row["成交数"]), int(row["未成交数"]), int(row["总数"])) == (hit, miss, total)
    assert row[alias] == rate
    assert res["task"]["task_type"] == "proportion"

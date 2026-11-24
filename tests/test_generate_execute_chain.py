# -*- coding: utf-8 -*-
"""端到端用户路径：描述 → 生成规则 → 执行。

模拟界面/CLI 的真实两步操作：
  1. 输入自然语言 intent，经 ``rules.generate_rules_from_dict`` 生成规则
     （FixtureProvider 提供模拟 LLM 输出；比率类由 apply_intent_plan 从描述
     程序化生成骨架，因此故意喂错误规则来验证「描述决定结果」）；
  2. 用生成的规则调用 ``execute_rule`` 完成读取→清洗→步骤→画像→报告→导出。

这与「手工构造规则直接执行」的测试不同：它覆盖生成阶段本身，
回答「换一个描述，生成的规则还能不能正确执行」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import rule_executor as rx
import rules
from tests.provider_fixture import FixtureProvider

COLS = ["市", "区县", "小区", "成交价(元)", "结束时间", "手机", "金额", "分组", "数值"]


def _data() -> pd.DataFrame:
    return pd.DataFrame({
        "市": ["福州"] * 10 + ["厦门"] * 6,
        "区县": ["鼓楼区", "台江区"] * 8,
        "小区": ["A", "B", "C", "A", "B", "C", "A", "B", "C", "A",
                 "D", "E", "D", "E", "D", "E"],
        # 福州 10 行：索引 1/4/8 为空 → 3 空 7 非空
        "成交价(元)": [100, None, 300, 400, None, 600, 700, 800, None, 1000,
                   1100, 1200, None, 1400, 1500, None],
        "结束时间": [
            "2026-03-08T10:00:00+08:00", "2026-07-01", "2025-01-01",
            "2026-12-31", "2026-05-05", "2026-02-02", "2026-01-01",
            "2026-01-02", "2026-01-03", "2026-01-04", "2026-06-01",
            "2026-06-02", "2025-12-31", "2026-08-08", "2026-09-09",
            "2026-10-10",
        ],
        "手机": ["13800138000"] * 16,
        "金额": ["1.2万", "1234", "100元", "3.5万元", "2000", "50", "1亿",
               "9999", "80", "90", "100", "110", "120", "130", "140", "150"],
        "分组": ["A", "B"] * 8,
        "数值": [1.0, None, 3.0, 10.0, 5.0, 2.0, 8.0, 4.0,
               6.0, 7.0, 9.0, 11.0, 12.0, 13.0, 14.0, 15.0],
    })


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "input.csv"
    _data().to_csv(path, index=False, encoding="utf-8-sig")
    return str(path)


def _generate(csv_path, intent, llm_steps, *, task_type="summary"):
    """走完整生成链路：模拟 LLM 输出 → 校验/规整 → 意图编译。"""
    payload = {"intent": intent, "source": {"path": csv_path, "format": "csv"},
               "columns": COLS, "generation_path": "legacy"}
    provider = FixtureProvider(rule={
        "version": "2.0", "task_type": task_type, "steps": llm_steps,
        "options": {"fill_missing": "median", "outlier_method": "none"},
    })
    return rules.generate_rules_from_dict(payload, provider=provider)


def _execute(rule, tmp_path, tag):
    rule = dict(rule)
    rule["output"] = {"formats": ["csv", "json"], "report": True}
    return rx.execute_rule(rule, base_output=str(tmp_path / tag))


def _frame(summary):
    return pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")


# ============================================================
# 比率类：故意喂最糟糕的 LLM 规则，验证「描述 → 程序化骨架 → 正确执行」
# ============================================================
WRONG_FILTERS = [
    {"op": "filter", "column": "成交价(元)", "operator": "isna"},
    {"op": "filter", "column": "成交价(元)", "operator": "notna"},
]

RATE_CASES = [
    {
        "name": "成交率-福州",
        "intent": ("筛选：市：福州 判断：成交价为空-未成交，成交价不为空-成交 "
                   "目标：统计成交率（成交/未成交）"),
        "alias": "成交率", "num": "hit",
        "expect": {"总数": 10, "成交数": 7, "未成交数": 3},
    },
    {
        "name": "未成交率-福州",
        "intent": ("筛选：市：福州 判断：成交价为空-未成交，成交价不为空-成交 "
                   "目标：统计未成交率（未成交/总数）"),
        "alias": "未成交率", "num": "miss",
        "expect": {"总数": 10, "成交数": 7, "未成交数": 3},
    },
    {
        "name": "成交率-鼓楼",
        "intent": ("筛选：区县：鼓楼区 判断：成交价为空-未成交，成交价不为空-成交 "
                   "目标：统计成交率（成交/未成交）"),
        "alias": "成交率", "num": "hit",
        "expect": {"总数": 8, "成交数": 5, "未成交数": 3},
    },
    {
        # condition=isna → hit 即「为空」，numerator=hit，两类标签为命中/未命中
        "name": "成交价为空率",
        "intent": "统计成交价为空率",
        "alias": "成交价为空率", "num": "hit",
        "expect": {"总数": 16, "命中数": 5, "未命中数": 11},
    },
]


@pytest.mark.parametrize("case", RATE_CASES, ids=[c["name"] for c in RATE_CASES])
def test_rate_description_generates_and_executes(case, csv_path, tmp_path):
    rule, meta = _generate(csv_path, case["intent"], WRONG_FILTERS, task_type="summary")
    # 生成阶段：比率骨架必须由描述触发，且覆盖错误的 LLM 规则
    assert meta.get("intent_plan", {}).get("applied") is True, meta
    rate = next(s for s in rule["steps"] if s["op"] == "rate")
    assert rate["alias"] == case["alias"]
    assert rate["numerator"] == case["num"]

    summary = _execute(rule, tmp_path, "rate_" + case["name"])
    assert summary["task"]["task_type"] == "proportion"
    assert all(s["status"] == "ok" for s in summary["step_logs"])
    frame = _frame(summary)
    for col, value in case["expect"].items():
        assert int(frame[col].iloc[0]) == value, (case["name"], col, frame.to_dict("records"))
    rate_value = float(frame[case["alias"]].iloc[0])
    assert 0 < rate_value < 100
    assert all(a["name"] for a in summary["artifacts"])


def test_correct_llm_rate_rule_is_preserved(csv_path):
    """模型答对时保留其算子链：确定性编译只做守门，不再无条件覆盖。"""
    from ops.intent import parse_rate_intent

    intent = "统计成交价为空率"
    spec = parse_rate_intent(intent, COLS)
    correct_steps = spec.steps() + [spec.rate_step()]
    rule, meta = _generate(csv_path, intent, correct_steps, task_type="proportion")
    assert meta.get("semantic_issues") == [], meta
    assert meta["intent_plan"]["decision"] == "preserved_correct_llm"
    assert meta["intent_plan"]["applied"] is False
    assert [step["op"] for step in rule["steps"]] == [step["op"] for step in correct_steps]
    assert rule["steps"][-1]["alias"] == "成交价为空率"


def test_correct_llm_rate_rule_rejects_bad_compiler_candidate(csv_path, monkeypatch):
    from ops.intent import parse_rate_intent

    intent = ("筛选：市：福州 判断：成交价为空-未成交，成交价不为空-成交 "
              "目标：统计成交率（成交/总数）")
    spec = parse_rate_intent(intent, COLS)
    correct_steps = spec.steps() + [spec.rate_step()]

    def bad_plan(rule, intent, columns, aliases=None, value_hints=None):
        candidate = dict(rule)
        candidate["steps"] = [{"op": "count_rows", "by": ["市"], "alias": "条数"}]
        candidate["task_type"] = "summary"
        return candidate, ["错误的编译提示"], {
            "applied": True, "spec": {"alias": "bad"}, "replaced_steps": 1,
        }

    monkeypatch.setattr(rules, "apply_intent_plan", bad_plan)
    rule, meta = _generate(csv_path, intent, correct_steps, task_type="proportion")
    assert meta["intent_plan"]["decision"] == "rejected_would_break_correct_llm"
    assert meta["intent_plan"]["applied"] is False
    assert rule["steps"] == correct_steps
    assert any("意图编译候选未采用" in w for w in meta.get("reconcile_warnings", []))


def test_grouped_rate_description_generates_and_executes(csv_path, tmp_path):
    intent = ("筛选：市：福州；判断：成交价为空-未成交，成交价不为空-成交；"
              "目标：按区县分组统计未成交率")
    rule, meta = _generate(csv_path, intent, WRONG_FILTERS)
    assert meta.get("intent_plan", {}).get("applied") is True
    rate = next(s for s in rule["steps"] if s["op"] == "rate")
    assert "区县" in rate.get("by", [])

    summary = _execute(rule, tmp_path, "group_rate")
    assert summary["task"]["task_type"] == "proportion"
    frame = _frame(summary)
    assert set(frame["区县"]) == {"鼓楼区", "台江区"}
    gulou = frame[frame["区县"] == "鼓楼区"].iloc[0]
    # 福州的鼓楼区为索引 0/2/4/6/8：3 条非空、2 条空
    assert (int(gulou["成交数"]), int(gulou["未成交数"])) == (3, 2)


# ============================================================
# 非比率类：模拟遵守提示词的 LLM 输出，验证生成链路 → 执行契约一致
# ============================================================
def _group_count_steps():
    return [
        {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
        {"op": "count_rows", "by": "小区", "alias": "条数"},
    ]


NON_RATE_CASES = [
    {
        "name": "分组计数",
        "intent": "筛选市：福州，按小区分组，统计每个小区有几条数据",
        "steps": _group_count_steps,
        "check": lambda s: _check_group_count(s),
    },
    {
        "name": "日期与判空",
        "intent": "筛选市是福州，小区不为空，结束时间在2026年，按小区统计条数",
        "steps": lambda: [
            {"op": "filter", "conditions": [
                {"column": "市", "operator": "eq", "value": "福州"},
                {"column": "小区", "operator": "notna"}], "logic": "and"},
            {"op": "date_filter", "column": "结束时间", "year": 2026},
            {"op": "count_rows", "by": "小区", "alias": "条数"},
        ],
        "check": lambda s: _check_date_filter(s),
    },
    {
        "name": "排序取前N",
        "intent": "按数值降序排序，取前5条",
        "steps": lambda: [
            {"op": "sort", "by": "数值", "ascending": False},
            {"op": "head", "n": 5},
        ],
        "check": lambda s: _check_head(s),
    },
    {
        "name": "脱敏",
        "intent": "对手机号做脱敏处理",
        "steps": lambda: [
            {"op": "mask_pii", "columns": ["手机"], "kinds": ["phone"], "mode": "partial"},
        ],
        "check": lambda s: _check_mask(s),
    },
    {
        "name": "单位换算",
        "intent": "把金额列换算为元，并规范化文本",
        "steps": lambda: [
            {"op": "convert_unit", "columns": ["金额"], "kind": "money"},
            {"op": "text_normalize", "columns": ["区县"]},
        ],
        "check": lambda s: None,
    },
    {
        "name": "透视表",
        "intent": "按分组和市做数值求和透视表",
        "steps": lambda: [
            {"op": "pivot", "index": "分组", "columns": "市",
             "values": "数值", "aggfunc": "sum"},
        ],
        "check": lambda s: None,
    },
]


def _check_group_count(summary):
    frame = _frame(summary)
    assert list(frame.columns) == ["小区", "条数"]
    assert int(frame["条数"].sum()) == 10


def _check_date_filter(summary):
    frame = _frame(summary)
    # 福州 10 行中 2025 年仅索引 2 一行，其余 9 行为 2026
    assert int(frame["条数"].sum()) == 9


def _check_head(summary):
    assert summary["rows"] == 5


def _check_mask(summary):
    frame = _frame(summary)
    assert not (frame["手机"].astype(str) == "13800138000").all()


@pytest.mark.parametrize("case", NON_RATE_CASES, ids=[c["name"] for c in NON_RATE_CASES])
def test_non_rate_description_generates_and_executes(case, csv_path, tmp_path):
    rule, meta = _generate(csv_path, case["intent"], case["steps"]())
    # 非比率类不应被意图骨架误改写
    assert not meta.get("intent_plan", {}).get("applied"), case["name"]
    summary = _execute(rule, tmp_path, "nonrate_" + case["name"])
    failed = [s for s in summary["step_logs"] if s["status"] != "ok"]
    assert not failed, (case["name"], failed)
    assert summary["rows"] > 0
    assert summary["task"]["task_type"]
    assert Path(summary["result_csv"]).exists()
    case["check"](summary)


# ============================================================
# 生成阶段失败必须显式报错，而不是悄悄编规则
# ============================================================
def test_generate_sanitizes_invalid_llm_operator(csv_path):
    """LLM 臆造非法算子时，生成链路白名单规整剔除并留告警，而不是把危险步骤带进执行。"""
    from ops.registry import op_names

    payload = {"intent": "按小区统计条数", "source": {"path": csv_path, "format": "csv"},
               "columns": COLS, "generation_path": "legacy"}
    provider = FixtureProvider(rule={
        "version": "2.0", "steps": [{"op": "__import__", "columns": []}],
        "options": {},
    })
    rule, meta = rules.generate_rules_from_dict(payload, provider=provider)
    assert all(s["op"] in op_names() for s in rule["steps"])
    assert meta.get("reconcile_warnings"), "规整动作必须可审计"

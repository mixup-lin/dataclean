# -*- coding: utf-8 -*-
"""回归：成交率（成交价为空=未成交 / 非空=成交）结果获取失败。

根因有两层：
1. LLM 规则写成 ``assign + count_rows``，只给计数不给比率，且 task_type 误标 summary；
2. 执行层清洗用中位数填充数值缺失，而 ``assign`` 的条件列没登记空值保护，
   2588 行全部被判成「成交」，只剩 1 组，比率无从计算。
修复：新增专用 ``rate`` 算子 + 编译器确定性归一 + 通用条件列空值还原 +
提示词口径约束 + 画像专用面板 + 跳过步骤可见告警。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import rule_executor as re
import rules
import task_profile
from ops.compiler import compile_rule
from ops.registry import get_op
from tests.provider_fixture import make_rule


OLD_STEPS = [
    {"op": "filter", "conditions": [
        {"column": "区县", "operator": "eq", "value": "鼓楼区"}], "logic": "and"},
    {"op": "assign", "column": "状态", "rules": [
        {"condition": {"column": "成交价(元)", "operator": "isna"}, "value": "未成交"},
        {"condition": {"column": "成交价(元)", "operator": "notna"}, "value": "成交"},
    ], "default": "未知"},
    {"op": "count_rows", "by": ["状态"], "alias": "条数"},
]


def _sample_csv(path: Path) -> str:
    frame = pd.DataFrame({
        "区县": ["鼓楼区"] * 6 + ["台江区"] * 2,
        "小区": ["A", "A", "B", "B", "C", "C", "D", "D"],
        "成交价(元)": [100.0, None, 200.0, None, 300.0, None, 400.0, 500.0],
    })
    target = path / "rate.csv"
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    return str(target)


# ============================================================
# 算子层
# ============================================================
def test_rate_operator_outputs_counts_rate_and_ratio():
    df = pd.DataFrame({"成交价(元)": [1.0, None, 2.0, None, 3.0]})
    out, note = re.apply_step(df, {
        "op": "rate", "column": "成交价(元)",
        "condition": {"column": "成交价(元)", "operator": "notna"},
        "true_label": "成交", "false_label": "未成交", "alias": "成交率",
    })
    row = out.iloc[0]
    assert list(out.columns) == ["成交数", "未成交数", "总数", "成交率", "成交未成交比"]
    assert (row["成交数"], row["未成交数"], row["总数"]) == (3, 2, 5)
    assert row["成交率"] == 60.0
    assert row["成交未成交比"] == 1.5
    assert "二分类比率" in note


def test_rate_operator_supports_group_by():
    df = pd.DataFrame({
        "区县": ["鼓楼区"] * 3 + ["台江区"] * 2,
        "成交价(元)": [1.0, None, None, 2.0, 3.0],
    })
    out, _ = re.apply_step(df, {
        "op": "rate", "column": "成交价(元)", "by": ["区县"],
        "true_label": "成交", "false_label": "未成交", "alias": "成交率",
    })
    assert len(out) == 2
    gulou = out[out["区县"] == "鼓楼区"].iloc[0]
    assert (gulou["成交数"], gulou["未成交数"], gulou["成交率"]) == (1, 2, 33.3333)


def test_rate_registered_and_compiles():
    assert get_op("rate") is not None and get_op("rate").role == "analysis"
    plan = compile_rule({"version": "2.0", "steps": [
        {"op": "rate", "column": "成交价(元)", "true_label": "成交",
         "false_label": "未成交", "alias": "成交率"},
    ]}, ["成交价(元)"])
    assert plan.steps[0]["op"] == "rate"
    assert "成交率" in plan.columns_out
    assert rules.validate_rule(make_rule(
        source_path="lots.csv",
        steps=[{"op": "rate", "column": "成交价(元)"}],
    ), ["成交价(元)"]) == []


# ============================================================
# 编译层：旧规则确定性纠偏
# ============================================================
def test_compiler_normalizes_assign_count_rows_to_rate():
    plan = compile_rule(
        {"version": "2.0", "steps": json.loads(json.dumps(OLD_STEPS))},
        ["区县", "成交价(元)"],
    )
    ops = [s["op"] for s in plan.steps]
    assert ops == ["filter", "rate"]
    rate = plan.steps[1]
    assert rate["column"] == "成交价(元)"
    assert rate["true_label"] == "成交" and rate["false_label"] == "未成交"
    assert any("归一为 rate" in w for w in plan.warnings)


def test_compiler_keeps_custom_chain_that_reuses_label_column():
    steps = OLD_STEPS + [
        {"op": "sort", "by": ["状态"], "ascending": [False]},
    ]
    plan = compile_rule({"version": "2.0", "steps": steps}, ["区县", "成交价(元)"])
    assert [s["op"] for s in plan.steps] == ["filter", "assign", "count_rows", "sort"]


def test_filter_columns_collects_assign_condition_columns():
    rule = {"steps": json.loads(json.dumps(OLD_STEPS))}
    cols = re._filter_columns(rule)
    assert "成交价(元)" in cols and "区县" in cols


# ============================================================
# 清洗层：原始空值语义还原（不再整列覆盖）
# ============================================================
def test_missing_masks_restore_only_emptiness_and_keep_text_cleaning():
    from etl_pipeline import PipelineConfig, clean_data

    raw = pd.DataFrame({
        "区县": [" 鼓楼区 ", None, "台江区"],
        "成交价(元)": [None, 200.0, 300.0],
    })
    masks = re._missing_masks(raw, {"区县", "成交价(元)"})
    cleaned, _ = clean_data(raw, PipelineConfig(
        input_path="dummy.csv", input_format="csv", fill_missing="median",
    ))
    re._restore_missing_masks(cleaned, masks)
    assert pd.isna(cleaned["成交价(元)"].iloc[0]), "填充的中位数必须被还原成空值"
    assert cleaned["区县"].iloc[0] == "鼓楼区", "文本归一成果应保留"
    assert pd.isna(cleaned["区县"].iloc[1]), "原始空文本列也应还原为空"


# ============================================================
# 端到端：旧规则重跑直接得到成交率
# ============================================================
def test_old_rule_rerun_produces_rate(tmp_path):
    source = _sample_csv(tmp_path)
    rule = make_rule(
        intent="筛选区县=鼓楼区，成交价为空=未成交，不为空=成交，统计成交率",
        source_path=source,
        steps=json.loads(json.dumps(OLD_STEPS)),
        options={"fill_missing": "median", "outlier_method": "none"},
    )
    rule["task_type"] = "summary"  # 旧规则的错误声明也应被自动纠正
    res = re.execute_rule(rule, base_output=str(tmp_path / "out"))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    assert res["task"]["task_type"] == "proportion"
    assert res["rows"] == 1
    assert list(frame.columns) == ["成交数", "未成交数", "总数", "成交率", "成交未成交比"]
    assert (int(frame["成交数"].iloc[0]), int(frame["未成交数"].iloc[0])) == (3, 3)
    assert frame["成交率"].iloc[0] == 50.0
    assert "成交率 50.00%" in res["task"]["conclusion"]
    assert any("归一为 rate" in w for w in res["reconcile_warnings"])
    assert res["safety_warnings"] == []
    assert [s["status"] for s in res["step_logs"]] == ["ok", "ok"]


def test_explicit_rate_rule_end_to_end(tmp_path):
    source = _sample_csv(tmp_path)
    rule = make_rule(
        source_path=source,
        steps=[
            {"op": "filter", "column": "区县", "operator": "eq", "value": "鼓楼区"},
            {"op": "rate", "column": "成交价(元)",
             "condition": {"column": "成交价(元)", "operator": "notna"},
             "true_label": "成交", "false_label": "未成交", "alias": "成交率"},
        ],
        options={"fill_missing": "median", "outlier_method": "none"},
    )
    rule["task_type"] = "proportion"
    res = re.execute_rule(rule, base_output=str(tmp_path / "out2"))
    frame = pd.read_csv(res["result_csv"], encoding="utf-8-sig")
    assert frame["成交率"].iloc[0] == 50.0
    assert res["task"]["title"] == "成交率分析"
    assert any(k["label"] == "成交/未成交" for k in res["task"]["kpis"])


def test_declared_summary_does_not_override_rate():
    rule = {"task_type": "summary", "steps": [
        {"op": "rate", "column": "成交价(元)"}]}
    assert task_profile.detect_task_type(rule) == "proportion"


def test_skipped_step_is_surfaced_as_warning(tmp_path, monkeypatch):
    source = _sample_csv(tmp_path)
    rule = make_rule(source_path=source,
                     steps=[{"op": "head", "n": 3}],
                     options={"outlier_method": "none"})
    def flaky(df, step, frames=None, ctx=None):
        raise RuntimeError("模拟算子失败")

    monkeypatch.setattr(re, "apply_step", flaky)
    res = re.execute_rule(rule, base_output=str(tmp_path / "out3"))
    assert res["step_logs"][0]["status"] == "skipped"
    assert any("未执行" in w and "结果可能不完整" in w
               for w in res["safety_warnings"])


def test_llm_prompt_describes_rate_contract():
    system, user = rules.build_rule_prompt(
        "统计鼓楼区成交率", {"path": "lots.csv", "format": "csv"},
        ["区县", "成交价(元)"],
    )
    assert '"op":"rate"' in system
    assert "成交率" in system and "严禁把这类需求写成 assign + count_rows" in system
    assert '"rate"' in user  # 算子目录与白名单一致

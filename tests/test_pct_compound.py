"""回归测试：复合条件占比 + 清洗后空值还原。

覆盖用户真实诉求：「筛选地区=福州、结束时间2026，统计所在楼层和总楼层都不为空
的数据占总数的比例」。根因曾有两处：
1. pct 的 condition 只支持单条条件，复合条件（A 和 B 都不为空）会解析失败；
2. 清洗阶段把缺失值填掉后，「非空」判断失效，占比恒为 100%。
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

import rule_executor as re
import rules


def _sample_df() -> pd.DataFrame:
    cols = ["小区", "地区", "结束时间", "所在楼层", "总楼层", "成交价"]
    data = [
        ["A", "福州", "2026-03-01", 3, 18, 100],
        ["B", "福州", "2026-07-15", None, 18, 200],
        ["C", "福州", "2025-11-01", 5, 22, 300],
        ["D", "厦门", "2026-05-01", 2, 9, 400],
        ["E", "福州", "2026-09-09", 7, None, 500],
        ["F", "福州", "2026-12-31", 1, 6, 600],
    ]
    return pd.DataFrame(data, columns=cols)


def test_pct_compound_condition_and():
    df = _sample_df()
    step = {
        "op": "pct", "scope": "condition",
        "condition": {"conditions": [
            {"column": "所在楼层", "operator": "notna"},
            {"column": "总楼层", "operator": "notna"}], "logic": "and"},
        "alias": "占比",
    }
    frame, _ = re._apply_pct(df, step)
    row = frame.iloc[0]
    assert int(row["分子"]) == 4
    assert int(row["分母"]) == 6
    assert round(float(row["占比"]), 4) == 66.6667


def test_pct_compound_condition_or():
    df = _sample_df()
    step = {
        "op": "pct", "scope": "condition",
        "condition": {"conditions": [
            {"column": "所在楼层", "operator": "isna"},
            {"column": "总楼层", "operator": "isna"}], "logic": "or"},
        "alias": "占比",
    }
    frame, _ = re._apply_pct(df, step)
    row = frame.iloc[0]
    # B(所在楼层空) 与 E(总楼层空) 命中 -> 2 / 6
    assert int(row["分子"]) == 2


def test_check_pct_accepts_compound_condition():
    known = {"所在楼层", "总楼层"}
    step = {"op": "pct", "scope": "condition",
            "condition": {"conditions": [
                {"column": "所在楼层", "operator": "notna"},
                {"column": "总楼层", "operator": "notna"}], "logic": "and"}}
    errs = []
    rules._check_pct(step, "steps[0]", known, errs)
    assert errs == []


def test_filter_columns_collects_pct_compound():
    rule = {"steps": [{
        "op": "pct", "scope": "condition",
        "condition": {"conditions": [
            {"column": "所在楼层", "operator": "notna"},
            {"column": "总楼层", "operator": "notna"}], "logic": "and"},
    }]}
    cols = re._filter_columns(rule)
    assert "所在楼层" in cols and "总楼层" in cols


def test_full_pipeline_preserves_nulls_for_ratio():
    """关键回归：清洗填缺失不能破坏「非空」占比。"""
    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "src.csv")
    _sample_df().to_csv(csv_path, index=False, encoding="utf-8-sig")

    rule = {
        "version": "2.0",
        "intent": "筛选地区是福州，结束时间在2026年的数据，然后统计其中所在楼层和总楼层都不为空的数据占总数的比例",
        "source": {"path": csv_path, "format": "csv"},
        "options": {"fill_missing": "median"},
        "steps": [
            {"op": "filter", "column": "地区", "operator": "eq", "value": "福州"},
            {"op": "date_part", "column": "结束时间", "parts": ["year"]},
            {"op": "filter", "column": "结束时间_年", "operator": "eq", "value": 2026},
            {"op": "pct", "scope": "condition",
             "condition": {"conditions": [
                 {"column": "所在楼层", "operator": "notna"},
                 {"column": "总楼层", "operator": "notna"}], "logic": "and"},
             "alias": "占比"},
        ],
        "output": {"formats": ["csv"], "report": True},
    }
    res = re.execute_rule(rule, base_output=tmp)
    assert res["task"]["task_type"] == "proportion"
    # 福州+2026 = 4 行；其中 所在楼层&总楼层 都非空 = 2 行 -> 50%
    assert "50.0%" in res["task"]["conclusion"]


def test_date_filter_replaces_date_part_for_year_condition():
    """P0 回归：2026 年筛选一步完成，不再派生 结束时间_年 中间列。"""
    import pandas as pd

    frame = _sample_df()
    out, note = re.apply_step(frame, {
        "op": "date_filter", "column": "结束时间", "year": 2026,
    })
    assert len(out) == 5  # 2025-11-01 那一行被排除
    assert "日期筛选" in note
    assert "结束时间_年" not in out.columns
    assert pd.api.types.is_datetime64_any_dtype(frame["结束时间"]) is False  # 原列未被改写


def test_compound_pct_explicit_rule_validates():
    """复合条件占比用显式规则表达，能通过注册表契约校验。"""
    from rules import assert_valid_rule
    from tests.provider_fixture import make_rule

    columns = ["小区", "地区", "结束时间", "所在楼层", "总楼层", "成交价", "面积", "户型"]
    steps = [
        {"op": "filter", "column": "地区", "operator": "eq", "value": "福州"},
        {"op": "date_filter", "column": "结束时间", "year": 2026},
        {"op": "pct", "scope": "condition",
         "condition": {"conditions": [
             {"column": "所在楼层", "operator": "notna"},
             {"column": "总楼层", "operator": "notna"}], "logic": "and"},
         "alias": "非空占比"},
    ]
    rule = make_rule(source_path="x.csv", steps=steps)
    assert_valid_rule(rule, columns)
    pct = rule["steps"][-1]
    assert {c["column"] for c in pct["condition"]["conditions"]} == {"所在楼层", "总楼层"}


def test_date_part_short_label_alias_fallback():
    """兼容旧约定：date_part 后用短名「年」过滤应被改写成 原列名_年。"""
    import rules as R
    rule = {
        "steps": [
            {"op": "date_part", "column": "结束时间", "parts": ["year"]},
            {"op": "filter", "column": "年", "operator": "eq", "value": 2026},
        ]
    }
    new_rule, warns = R.reconcile_rule_columns(rule, ["结束时间"])
    assert warns == []
    assert new_rule["steps"][1]["column"] == "结束时间_年"

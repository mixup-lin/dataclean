# -*- coding: utf-8 -*-
"""筛选语义回归：日期列年份筛选、时区边界、分组计数误判。

真实缺陷（run_20261115_200052）：LLM 把「结束时间在2026年」写成
``{"column":"结束时间","operator":"eq","value":"2026"}``，而该列真实值是
``2026-03-08T10:00:00+08:00``，字符串全等恒为 False → 19954 行筛成 0 行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rule_executor import apply_step  # noqa: E402
from task_profile import detect_task_type  # noqa: E402


@pytest.fixture
def frame() -> pd.DataFrame:
    """ replicates lots.csv 的关键列形态：市全为福州、ISO 带时区时间戳、小区含空值。"""
    return pd.DataFrame({
        "市": ["福州"] * 8 + ["厦门"],
        "结束时间": [
            "2026-03-08T10:00:00+08:00",
            "2026-01-02T10:00:00+08:00",
            "2026-12-31T10:00:00+08:00",
            "2025-12-31T10:00:00+08:00",
            "2027-01-01T10:00:00+08:00",
            "2026-07-01T10:00:00+08:00",
            None,
            "2026-05-05T10:00:00+08:00",
            "2026-09-09T10:00:00+08:00",
        ],
        "小区": ["A", "A", None, "B", "B", "C", "A", None, "A"],
        "建成年份": [2017, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
    })


def test_year_equality_on_datetime_column(frame):
    """eq 一个 4 位年份，应解释为「年份相等」而不是字符串全等。"""
    step = {"op": "filter", "column": "结束时间", "operator": "eq", "value": "2026"}
    out, _ = apply_step(frame, step)
    assert len(out) == 6                      # 2026 的 6 行（含厦门那行）
    assert set(out["结束时间"].dropna().str[:4]) == {"2026"}


def test_year_equality_accepts_int_value(frame):
    step = {"op": "filter", "column": "结束时间", "operator": "eq", "value": 2026}
    out, _ = apply_step(frame, step)
    assert len(out) == 6


def test_year_equality_ne_operator(frame):
    out, _ = apply_step(
        frame, {"op": "filter", "column": "结束时间", "operator": "ne", "value": "2026"}
    )
    assert len(out) == 3


def test_year_rewrite_does_not_hit_plain_numeric_column(frame):
    """纯年份数值列必须保持原来的数值比较语义，不能被年份抽取改写坏。"""
    out, _ = apply_step(
        frame, {"op": "filter", "column": "建成年份", "operator": "eq", "value": "2017"}
    )
    assert len(out) == 2


def test_date_filter_year_and_range_agree(frame):
    """start/end 区间在 tz-aware 数据上不得抛 TypeError，且与 year 口径一致。"""
    by_year, _ = apply_step(
        frame, {"op": "date_filter", "column": "结束时间", "year": 2026}
    )
    by_range, _ = apply_step(
        frame, {"op": "date_filter", "column": "结束时间",
                "start": "2026-01-01", "end": "2026-12-31"}
    )
    assert len(by_year) == len(by_range) == 6
    assert list(by_year.index) == list(by_range.index)


def test_date_filter_recent_days_no_tz_error(frame):
    out, note = apply_step(
        frame, {"op": "date_filter", "column": "结束时间", "recent_days": 3650}
    )
    assert "近 3650 天" in note
    assert len(out) <= len(frame)


def test_group_count_intent_returns_rows_and_summary(frame):
    """用户原始诉求的完整链路：市=福州 + 2026年 + 小区非空 → 按小区计数。"""
    steps = [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "结束时间", "operator": "eq", "value": "2026"},
            {"column": "小区", "operator": "notna"},
        ], "logic": "and"},
        {"op": "count_rows", "by": "小区", "alias": "条数"},
    ]
    cur = frame
    for step in steps:
        cur, _ = apply_step(cur, step)
    assert len(cur) > 0                       # 核心断言：不再是 0
    assert len(cur) == 2                      # A / C 两个小区
    assert set(cur["小区"]) == {"A", "C"}
    assert int(cur["条数"].sum()) == 3        # 命中的 3 行（0/1/5）
    assert detect_task_type({"steps": steps}, cur) == "summary"


def test_recommended_date_filter_chain(frame):
    """提示词推荐的写法（date_filter + count_rows）同样正确。"""
    steps = [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "小区", "operator": "notna"},
        ], "logic": "and"},
        {"op": "date_filter", "column": "结束时间", "year": 2026},
        {"op": "count_rows", "by": "小区", "alias": "条数"},
    ]
    cur = frame
    for step in steps:
        cur, _ = apply_step(cur, step)
    assert int(cur["条数"].sum()) > 0
    assert detect_task_type({"steps": steps}, cur) == "summary"


def test_date_filter_handles_mixed_tz_and_naive_dates():
    """pandas 3.x 的 format='mixed' 会把混合列里的 naive 日期解析成 NaT。"""
    df = pd.DataFrame({
        "标签": ["a", "b", "c", "d", "e"],
        "日期": [
            "2026-03-08T10:00:00+08:00",  # tz-aware
            "2026-07-01",                 # naive
            "2025-01-01",                 # naive / 非 2026
            "2026-12-31",                 # naive / 年末
            None,                         # 缺失
        ],
    })
    out, _ = apply_step(
        df, {"op": "date_filter", "column": "日期", "year": 2026}
    )
    assert list(out["标签"]) == ["a", "b", "d"], "带时区与不带时区的 2026 年都应保留"


def test_prompt_forbids_year_equality_on_raw_datetime():
    from rules import _RULES_SYSTEM

    assert "严禁对原始日期时间列用 eq/ne 搭配 4 位年份字符串" in _RULES_SYSTEM
    assert '{"op":"date_filter","column":"结束时间","year":2026}' in _RULES_SYSTEM
    assert "纯分组计数绝不是 distribution" in _RULES_SYSTEM

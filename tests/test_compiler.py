"""编译/降解层测试：别名回退、缺列剔除、错误聚合、v2 契约。"""
from __future__ import annotations

import pytest

from ops.compiler import compile_rule
from ops.errors import RuleValidationError


def test_compile_date_part_short_label_alias():
    rule = {
        "version": "2.0",
        "steps": [
            {"op": "date_part", "column": "结束时间", "parts": ["year"]},
            {"op": "filter", "column": "年", "operator": "eq", "value": 2026},
        ],
    }
    plan = compile_rule(rule, ["结束时间"])
    assert plan.warnings == []
    assert plan.steps[1]["column"] == "结束时间_年"
    assert "结束时间_年" in plan.columns_out


def test_compile_prunes_step_with_missing_column():
    rule = {
        "version": "2.0",
        "steps": [
            {"op": "filter", "column": "不存在的列", "operator": "eq", "value": 1},
            {"op": "head", "n": 2},
        ],
    }
    plan = compile_rule(rule, ["a"])
    assert [s["op"] for s in plan.steps] == ["head"]
    assert plan.warnings and "不存在的列" in plan.warnings[0]


def test_compile_aggregates_multiple_errors():
    rule = {
        "version": "2.0",
        "steps": [
            {"op": "no_such_op"},
            {"op": "head"},          # 缺少必填 n
        ],
    }
    with pytest.raises(RuleValidationError) as exc:
        compile_rule(rule, ["a"])
    assert len(exc.value.errors) >= 2


def test_compile_type_error_detected():
    rule = {"version": "2.0", "steps": [{"op": "head", "n": "not-int"}]}
    with pytest.raises(RuleValidationError) as exc:
        compile_rule(rule, ["a"])
    assert any("类型错误" in e for e in exc.value.errors)


def test_compile_pct_compound_condition_passes():
    rule = {
        "version": "2.0",
        "steps": [{
            "op": "pct", "scope": "condition",
            "condition": {"conditions": [
                {"column": "所在楼层", "operator": "notna"},
                {"column": "总楼层", "operator": "notna"},
            ], "logic": "and"},
            "alias": "占比",
        }],
    }
    plan = compile_rule(rule, ["所在楼层", "总楼层"])
    assert plan.steps[0]["op"] == "pct"
    assert "分子" in plan.columns_out and "占比" in plan.columns_out


def test_compile_keeps_version():
    plan = compile_rule({"version": "2.0", "steps": []}, ["a"])
    assert plan.version == "2.0"
    assert plan.to_dict()["version"] == "2.0"

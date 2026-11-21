# -*- coding: utf-8 -*-
"""ops/compiler.py 的比率意图读取从中文正则迁移到 Schema 字段的回归测试。"""
from __future__ import annotations

from typing import Any, Dict, List

from ops.compiler import compile_rule
from ops.intent import parse_rate_intent, target_from_intent
from ops.intent_schema import rate_spec_from_schema, target_from_schema

COLUMNS = ["标的号", "市", "成交价(元)", "结束时间"]


def _schema(alias: str = "成交率", numerator: str = "hit", by: List[str] | None = None) -> Dict[str, Any]:
    op: Dict[str, Any] = {
        "kind": "rate", "column": "成交价(元)",
        "condition": {"column": "成交价(元)", "operator": "notna"},
        "true_label": "成交", "false_label": "未成交",
        "target_label": alias.removesuffix("率"),
        "alias": alias, "numerator": numerator, "denominator": "total",
    }
    if by:
        op["by"] = by
    return {"version": "1.0", "task_type": "proportion", "operations": [op], "assumptions": []}


def _rule(alias: str = "成交率", numerator: str = "miss",
          intent: str = "统计成交率：成交价不为空算成交，为空算未成交") -> Dict[str, Any]:
    return {
        "version": "2.0", "intent": intent, "task_type": "proportion",
        "source": {"path": "x.csv", "format": "csv"},
        "options": {},
        "steps": [{
            "op": "rate", "column": "成交价(元)",
            "condition": {"column": "成交价(元)", "operator": "notna"},
            "true_label": "成交", "false_label": "未成交",
            "alias": alias, "numerator": numerator, "denominator": "total",
        }],
        "output": {"formats": ["csv"], "report": True},
    }


def test_schema_rate_helpers_read_structured_fields_only() -> None:
    schema = _schema("未成交率", "miss", ["市"])
    spec = rate_spec_from_schema(schema, COLUMNS)
    assert spec is not None
    assert spec.judge_column == "成交价(元)"
    assert spec.alias == "未成交率"
    assert spec.numerator == "miss"
    assert spec.by == ["市"]
    assert "Schema" in "".join(spec.notes)
    assert target_from_schema(schema) == {"alias": "未成交率", "target_label": "未成交"}


def test_compile_rule_prefers_schema_over_conflicting_intent_text() -> None:
    """Schema 明确写成交率时，即使 intent 文本写未成交率，也不能再被正则带偏。"""
    rule = _rule(alias="成交率", numerator="miss", intent="统计未成交率")
    rule["intent_schema"] = _schema("成交率", "hit")
    plan = compile_rule(rule, COLUMNS)
    rate = next(s for s in plan.steps if s.get("op") == "rate")
    assert rate["alias"] == "成交率"
    assert rate["numerator"] == "hit"


def test_compile_rule_uses_schema_by_field() -> None:
    rule = _rule(numerator="hit")
    rule["intent_schema"] = _schema("成交率", "hit", ["市"])
    plan = compile_rule(rule, COLUMNS)
    rate = next(s for s in plan.steps if s.get("op") == "rate")
    assert rate["by"] == ["市"]


def test_legacy_rule_without_schema_still_uses_text_fallback() -> None:
    intent = "统计未成交率：成交价为空算未成交，不为空算成交"
    legacy = _rule(alias="成交率", numerator="hit", intent=intent)
    plan = compile_rule(legacy, COLUMNS)
    rate = next(s for s in plan.steps if s.get("op") == "rate")
    assert rate["alias"] == "未成交率"
    assert rate["numerator"] == "miss"
    # 旧入口保持可用，迁移不是破坏性替换。
    assert parse_rate_intent(intent, COLUMNS) is not None
    assert target_from_intent(intent) == {"alias": "未成交率", "target_label": "未成交"}


def test_invalid_schema_payload_does_not_raise() -> None:
    assert target_from_schema({"operations": "bad"}) is None
    assert rate_spec_from_schema({"operations": [{"kind": "rate"}]}, COLUMNS) is None

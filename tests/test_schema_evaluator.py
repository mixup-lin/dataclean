# -*- coding: utf-8 -*-
"""结构化 Schema  evaluator 的离线单元测试（不调用真实模型）。

验证主路径规则只经过结构复评，且 Schema 校验会给出可供评测器消费的结构化错误码。
"""
from __future__ import annotations

from typing import Any, Dict

import pandas as pd

import rules
import webapp
from llm_provider import LLMError
from ops.intent_schema import SCHEMA_VERSION, compile_schema, validate_schema
from ops.rule_eval import evaluate_rule

COLS = ["标的号", "面积(㎡)", "成交价(元)", "结束时间"]


def _rule(schema_payload: Dict[str, Any]) -> Dict[str, Any]:
    result = validate_schema(schema_payload, COLS)
    assert result.ok, result.errors
    compiled = compile_schema(result)
    return {
        "version": "2.0",
        "intent": "任意中文描述（结构化复评不应读取）",
        "source": {"path": "x.csv", "format": "csv"},
        "options": {"fill_missing": "none"},
        "task_type": compiled.task_type,
        "steps": compiled.steps,
        "intent_schema": result.to_dict(),
        "output": {"formats": ["csv"], "report": True},
    }


def test_schema_rule_evaluates_without_chinese_keywords() -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "task_type": "trend",
        "operations": [
            {"kind": "date_part", "column": "结束时间", "parts": ["year", "month"],
             "bucket": "年月"},
            {"kind": "aggregate", "by": ["结束时间_年月"],
             "metrics": [{"kind": "ratio", "numerator": "成交价(元)",
                          "denominator": "面积(㎡)", "alias": "均价",
                          "func": "sum_div_sum"}]},
        ],
        "assumptions": [],
    }
    rule = _rule(payload)
    # 描述里故意不出现任何需求关键词；结构复评仍应通过。
    assert evaluate_rule(rule, "随机数", COLS) == []


def test_schema_rule_detects_step_tampering() -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "task_type": "detail",
        "operations": [
            {"kind": "filter", "conditions": [
                {"column": "面积(㎡)", "operator": "gt", "value": 1000}], "logic": "and"},
        ],
        "assumptions": [],
    }
    rule = _rule(payload)
    rule["steps"].append({"op": "aggregate", "by": [], "column": ["成交价(元)"],
                          "func": ["mean"], "alias": ["均价"]})
    issues = evaluate_rule(rule, "提取面积大于1000的数据", COLS)
    assert any(i.code == "schema_steps_mismatch" for i in issues), issues


def test_schema_validation_returns_structured_codes() -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "task_type": "summary",
        "operations": [{"kind": "aggregate", "by": ["不存在的列"],
                        "metrics": [{"column": "成交价(元)", "func": "mean", "alias": "均价"}]}],
        "assumptions": [],
    }
    result = validate_schema(payload, COLS)
    assert not result.ok
    assert "unknown_column" in result.error_codes, result.error_codes

    rejected = {
        "version": SCHEMA_VERSION, "task_type": "detail",
        "operations": [{"kind": "select", "columns": ["标的号"]}],
        "assumptions": [], "unresolved_columns": ["不存在的字段"],
    }
    result2 = validate_schema(rejected, COLS)
    assert "unresolved_columns" in result2.error_codes, result2.error_codes

    err = LLMError("失败", "parse", codes=result2.error_codes)
    assert set(err.codes) == {"unresolved_columns"}


def test_workbench_renders_schema_panel_by_default(tmp_path, monkeypatch) -> None:
    """前端默认主路径：/plan 展示 Schema 面板，且不再暴露 opt-in 开关。"""
    import json

    csv = tmp_path / "in.csv"
    pd.DataFrame({
        "标的号": ["A1", "A2"], "面积(㎡)": [20, 2000],
        "成交价(元)": [100, 300], "结束时间": ["2026-01-15", "2026-02-15"],
    }).to_csv(csv, index=False, encoding="utf-8-sig")
    payload = {
        "version": SCHEMA_VERSION, "task_type": "detail",
        "operations": [{"kind": "filter", "conditions": [
            {"column": "面积(㎡)", "operator": "gt", "value": 1000}], "logic": "and"}],
        "assumptions": [], "unresolved_columns": [],
    }

    class Provider:
        name = "scripted"
        mode = "online"
        model = "scripted-v1"
        reason = ""

        def complete(self, system: str, user: str) -> str:
            return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(rules, "get_provider", lambda: Provider())
    state = webapp.AppState(str(tmp_path / "out"))
    reply = webapp.handle_request("POST", "/plan", {}, {
        "intent": ["提取面积大于1000的数据"],
        "source_path": [str(csv)], "source_format": ["csv"],
        "columns": [",".join(COLS)], "formats": ["csv"], "report": ["1"],
    }, state)
    assert reply.status == 200, reply.body[:800]
    assert "schema-panel" in reply.body
    assert "op-chip" in reply.body and "filter" in reply.body
    assert 'name="schema_mode"' not in reply.body
    assert "主路径" in reply.body

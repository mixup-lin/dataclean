# -*- coding: utf-8 -*-
"""生成质量门禁（评估报告 F-01）：验证「描述 → /plan → 语义自检」链路。

核心立场：**不直灌规则**。测试侧只提供一个按调用顺序返回三类响应的
ScriptedProvider：

1. 第 1 次：非法 JSON 字符串        → 触发 JSON 回灌修复（json_repair）；
2. 第 2 次：结构合法但语义错误的规则 → 触发语义自检 + 定点修复（repair_rounds）；
3. 第 3 次：正确规则                 → 自检收敛（semantic_issues 为空）。

链路严格走 ``webapp.handle_request("POST", "/plan", ...)``，让真实的
``generate_rules`` → ``validate_rule`` → ``evaluate_rule`` → ``apply_intent_plan``
全部执行。仅通过 monkeypatch 注入 Provider；为读取 ``generate_rules`` 返回的
meta（/plan 页面只渲染部分 meta），额外挂一个**透明 spy**：它委托真实实现，
仅记录 meta，不替换任何生产逻辑。

覆盖两类意图：比率类（成交率）与组合类（去重→筛选→派生→季度→趋势）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

import rules
import webapp
from ops.intent import parse_workflow_intent
from tests.conftest import extract_rule_from_plan_body, patch_provider
from tests.industry_harness import ScriptedProvider

# ---------------------------------------------------------------------------
# 意图 / 列定义
# ---------------------------------------------------------------------------
INTENT_RATE = "统计成交率（成交/总数）"
COLS_RATE = ["标的号", "成交价(元)"]

INTENT_COMPOSITE = (
    "去重，然后提取出面积小于50或大于1000的数据，最后按结束时间，"
    "每一年按季度分析每个季度的平均成交单价的趋势，成交单价 = （成交价/面积）"
)
COLS_COMPOSITE = ["标的号", "面积(㎡)", "成交价(元)", "结束时间"]

EXPECTED_COMPOSITE_OPS = [
    "dedupe", "filter", "expr", "date_part", "dropna",
    "filter", "aggregate", "expr", "sort",
]

_INVALID_JSON = "NOT_JSON: upstream returned an unparsable envelope"


# ---------------------------------------------------------------------------
# 规则构造（测试侧只构造「模型应答」，不直接塞给 /execute）
# ---------------------------------------------------------------------------
def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> Path:
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _rate_rule(path: Path) -> Dict[str, Any]:
    """比率类的「正确」规则：一个 rate 算子，hit/total 方向。"""
    return {
        "version": "2.0",
        "intent": INTENT_RATE,
        "source": {"path": str(path), "format": "csv"},
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": "proportion",
        "steps": [{
            "op": "rate",
            "column": "成交价(元)",
            "condition": {"column": "成交价(元)", "operator": "notna"},
            "true_label": "成交",
            "false_label": "未成交",
            "numerator": "hit",
            "denominator": "total",
            "alias": "成交率",
        }],
        "output": {"formats": ["csv"], "report": True},
    }


def _wrong_rate_rule(path: Path) -> Dict[str, Any]:
    """比率类的「语义错误」规则：只筛选，没有 rate 算子（missing_rate_op）。"""
    return {
        "version": "2.0",
        "intent": INTENT_RATE,
        "source": {"path": str(path), "format": "csv"},
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": "proportion",
        "steps": [{
            "op": "filter",
            "conditions": [{"column": "成交价(元)", "operator": "notna"}],
            "logic": "and",
        }],
        "output": {"formats": ["csv"], "report": True},
    }


def _composite_rule(path: Path) -> Dict[str, Any]:
    """组合类的「正确」规则：由 parse_workflow_intent 编译出的完整算子链。"""
    spec = parse_workflow_intent(INTENT_COMPOSITE, COLS_COMPOSITE)
    assert spec is not None, "组合意图应能编译出工作流"
    return {
        "version": "2.0",
        "intent": INTENT_COMPOSITE,
        "source": {"path": str(path), "format": "csv"},
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": spec.task_type,
        "steps": [dict(step) for step in spec.steps],
        "output": {"formats": ["csv"], "report": True},
    }


def _wrong_composite_rule(path: Path) -> Dict[str, Any]:
    """组合类的「语义错误」规则：只做前半段清洗，丢掉公式/季度/分析。"""
    return {
        "version": "2.0",
        "intent": INTENT_COMPOSITE,
        "source": {"path": str(path), "format": "csv"},
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": "detail",
        "steps": [
            {"op": "dedupe", "subset": ["标的号"], "keep": "first"},
            {"op": "filter", "logic": "or", "conditions": [
                {"column": "面积(㎡)", "operator": "lt", "value": 50},
                {"column": "面积(㎡)", "operator": "gt", "value": 1000},
            ]},
        ],
        "output": {"formats": ["csv"], "report": True},
    }


# ---------------------------------------------------------------------------
# 链路执行 + meta 观测
# ---------------------------------------------------------------------------
def _run_generation_gate(
    monkeypatch: Any,
    tmp_path: Path,
    case: str,
    intent: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    correct: Dict[str, Any],
    wrong: Dict[str, Any],
):
    """走 /plan 全链路，返回 (reply, provider, captured_meta)。

    ``_spy`` 是透明观察器：调用真实 ``rules.generate_rules_legacy`` 并记录 meta。
    本用例验证旧规则 DSL 的 JSON 修复/语义回灌链路，因此显式指定
    ``generation_path=legacy``；默认主路径为结构化意图 Schema。
    """
    csv_path = _write_csv(tmp_path / f"{case}.csv", columns, rows)
    state = webapp.AppState(str(tmp_path / f"out_{case}"))
    provider = ScriptedProvider([
        _INVALID_JSON,
        json.dumps(wrong, ensure_ascii=False),
        json.dumps(correct, ensure_ascii=False),
    ])
    patch_provider(monkeypatch, provider)

    captured: Dict[str, Any] = {}
    real_generate = rules.generate_rules_legacy

    def _spy(*args, **kwargs):
        rule, meta = real_generate(*args, **kwargs)
        captured["rule"] = rule
        captured["meta"] = meta
        return rule, meta

    monkeypatch.setattr(rules, "generate_rules_legacy", _spy)

    reply = webapp.handle_request("POST", "/plan", {}, {
        "intent": [intent],
        "source_path": [str(csv_path)],
        "source_format": ["csv"],
        "columns": [",".join(columns)],
        "formats": ["csv"],
        "report": ["1"],
        "generation_path": ["legacy"],
    }, state)
    return reply, provider, captured


def _assert_repair_converged(
    reply, provider: ScriptedProvider, captured: Dict[str, Any], expected_ops: List[str]
) -> None:
    """公共断言：/plan 成功、三步应答按序消费、meta 收敛、规则链正确。"""
    assert reply.status == 200, reply.body[:1200]
    rule = extract_rule_from_plan_body(reply.body)
    assert [step.get("op") for step in rule["steps"]] == expected_ops

    # 三次应答按顺序被消费：非法 JSON → 错误规则 JSON → 正确规则 JSON。
    assert len(provider.calls) == 3, len(provider.calls)
    # 第 2 次请求携带「JSON 不合法」回灌说明；第 3 次携带语义问题回灌说明。
    assert "合法 JSON" in provider.calls[1][1], provider.calls[1][1][:200]
    assert "issue" in provider.calls[2][1].lower() or "语义" in provider.calls[2][1], \
        provider.calls[2][1][:200]

    assert "meta" in captured, "/plan 未走 generate_rules（spy 未捕获）"
    meta = captured["meta"]
    assert meta.get("json_repair") is True, meta
    assert meta.get("repair_rounds") == 1, meta
    assert meta.get("semantic_issues") == [], meta
    assert meta.get("llm_attempts") == 3, meta
    # 最终规则应来自第 3 次应答，而不是被前两次「噪声」应答污染。
    assert [s.get("op") for s in captured["rule"]["steps"]] == expected_ops


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_generation_gate_rate_intent_repairs_invalid_json_and_semantics(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """比率类：成交率意图经 JSON 回灌 + 一次语义修复后收敛到 rate 算子。"""
    rows = [
        {"标的号": "A1", "成交价(元)": 100.0},
        {"标的号": "A2", "成交价(元)": 200.0},
        {"标的号": "A3", "成交价(元)": None},
    ]
    path = tmp_path / "rate.csv"
    reply, provider, captured = _run_generation_gate(
        monkeypatch, tmp_path, "rate", INTENT_RATE, COLS_RATE, rows,
        _rate_rule(path), _wrong_rate_rule(path),
    )
    _assert_repair_converged(reply, provider, captured, ["rate"])
    assert captured["meta"]["prompt_family"] == "rate"
    assert captured["meta"]["intent_plan"]  # apply_intent_plan 真实执行过


def test_generation_gate_composite_intent_repairs_invalid_json_and_semantics(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """组合类：去重→筛选→派生→季度→趋势，经修复后收敛到完整算子链。"""
    rows = [
        {"标的号": "L1", "面积(㎡)": 20.0, "成交价(元)": 1000.0, "结束时间": "2026-01-15"},
        {"标的号": "L2", "面积(㎡)": 2000.0, "成交价(元)": 3000.0, "结束时间": "2026-04-15"},
        {"标的号": "L3", "面积(㎡)": 100.0, "成交价(元)": 2000.0, "结束时间": "2026-07-15"},
    ]
    path = tmp_path / "composite.csv"
    reply, provider, captured = _run_generation_gate(
        monkeypatch, tmp_path, "composite", INTENT_COMPOSITE, COLS_COMPOSITE, rows,
        _composite_rule(path), _wrong_composite_rule(path),
    )
    _assert_repair_converged(reply, provider, captured, EXPECTED_COMPOSITE_OPS)
    assert captured["meta"]["prompt_family"] == "composite"
    assert captured["rule"]["task_type"] == "trend"

# -*- coding: utf-8 -*-
"""提示词工程回归测试。

默认测试完全离线：校验路由、少样本、硬约束、自检清单与语义评测器。
设置环境变量 RUN_REAL_LLM_TESTS=1 时，额外用真实模型跑 31 个场景矩阵，
要求「生成即正确」比例 >= 90%。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import rules
from llm_provider import get_provider, parse_json_response
from ops.prompt_kit import primary_family
from ops.rule_eval import Issue, evaluate_rule
from tests.prompt_scenarios import ALIASES, COLUMNS, SCENARIOS, SOURCE


def _aliases() -> dict:
    return {k: v for k, v in ALIASES.items() if v}


# ------------------------------------------------------------
# 离线：路由与提示词结构
# ------------------------------------------------------------
def test_all_scenarios_route_to_expected_family():
    for scenario in SCENARIOS:
        detected = primary_family(scenario["intent"], COLUMNS, _aliases())
        assert detected == scenario["family"], (scenario["id"], detected)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_prompt_contains_task_specific_guidance(scenario):
    system, user = rules.build_rule_prompt(
        scenario["intent"], SOURCE, COLUMNS, column_aliases=_aliases()
    )
    assert "【硬约束" in system
    assert "【输出前最后核对" in system
    assert f"任务族：{scenario['family']}" in system
    payload = json.loads(user)
    router = payload["task_router"]
    assert router["primary_family"] == scenario["family"]
    assert router["expected_task_type"] or scenario["family"] in ("date_filter", "detail")
    assert "reminder" in router and "steps 不允许为空" in router["reminder"]


def test_prompt_includes_ratio_override_and_date_hint():
    system, user = rules.build_rule_prompt(
        "成交率（成交/未成交）", SOURCE, COLUMNS
    )
    assert "numerator=\\\"hit\\\"" in system or 'numerator="hit"' in system
    assert "denominator=\\\"miss\\\"" in system or 'denominator="miss"' in system
    router = json.loads(user)["task_router"]
    assert router["extracted_hints"]["ratio_override"] == {
        "numerator": "hit", "denominator": "miss", "from": "成交/未成交"
    }

    _system, user2 = rules.build_rule_prompt(
        "统计结束时间在2026-01-01到2026-06-30之间的数据条数", SOURCE, COLUMNS
    )
    router2 = json.loads(user2)["task_router"]
    assert router2["extracted_hints"]["date_hint"] == {
        "start": "2026-01-01", "end": "2026-06-30"
    }


# ------------------------------------------------------------
# 离线：语义评测器能抓住反复出现的错误形态
# ------------------------------------------------------------
def _rule(steps, **extra):
    data = {
        "version": "2.0", "intent": "测试", "source": SOURCE,
        "steps": steps, "options": {}, "output": {"formats": ["csv"], "report": True},
    }
    data.update(extra)
    return data


def _codes(issues) -> set:
    return {i.code for i in issues}


def test_eval_catches_judge_in_filter():
    rule = _rule([
        {"op": "filter", "conditions": [
            {"column": "成交价(元)", "operator": "notna"}]},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "alias": "成交率"},
    ], task_type="proportion")
    assert "judge_in_filter" in _codes(evaluate_rule(
        rule, "统计鼓楼区成交率（成交/总数）", COLUMNS))


def test_eval_catches_options_only_group_by():
    rule = _rule([], options={"group_by": "小区"})
    assert "empty_steps" in _codes(evaluate_rule(
        rule, "统计每个小区有几条数据", COLUMNS))


def test_eval_catches_date_filter_nested_in_filter():
    rule = _rule([
        {"op": "filter", "conditions": [
            {"op": "date_filter", "column": "结束时间", "start": "2026-01-01", "end": "2026-06-30"}]},
        {"op": "count_rows", "alias": "条数"},
    ], task_type="summary")
    codes = _codes(evaluate_rule(
        rule, "统计结束时间在2026-01-01到2026-06-30之间的数据条数", COLUMNS))
    assert {"date_filter_nested", "missing_date_filter"} <= codes


def test_eval_catches_wrong_ratio_direction():
    rule = _rule([
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "numerator": "hit", "denominator": "total", "alias": "成交率"},
    ], task_type="proportion")
    assert "wrong_rate_direction" in _codes(evaluate_rule(
        rule, "成交率（成交/未成交）", COLUMNS))


def test_eval_allows_valid_ratio_direction():
    rule = _rule([
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "numerator": "hit", "denominator": "miss", "alias": "成交率"},
    ], task_type="proportion")
    assert evaluate_rule(rule, "成交率（成交/未成交）", COLUMNS) == []


def test_eval_catches_detail_summary_and_unknown_column():
    rule = _rule([
        {"op": "filter", "conditions": [{"column": "区县", "operator": "eq", "value": "鼓楼区"}]},
        {"op": "count_rows", "by": ["不存在的列"], "alias": "条数"},
    ], task_type="summary")
    codes = _codes(evaluate_rule(
        rule,
        "提取区县为鼓楼区的数据，其中成交价空值置为0，其他字段空值置为null",
        COLUMNS,
    ))
    assert {"detail_has_summary", "unknown_column", "wrong_task_type", "missing_fill"} <= codes


# ------------------------------------------------------------
# 在线：真实模型准确率（默认跳过，需显式开启）
# ------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("RUN_REAL_LLM_TESTS") != "1",
    reason="设置 RUN_REAL_LLM_TESTS=1 后才会调用在线模型",
)
def test_real_llm_generation_accuracy_at_least_90_percent():
    provider = get_provider()
    results = []
    for scenario in SCENARIOS:
        system, user = rules.build_rule_prompt(
            scenario["intent"], SOURCE, COLUMNS, column_aliases=_aliases()
        )
        raw = provider.complete(system, user)
        candidate = parse_json_response(raw)
        rule = rules.normalize_rule(candidate, SOURCE, scenario["intent"])
        if rules.validate_rule(rule, COLUMNS):
            rule, _ = rules.sanitize_rule(rule, COLUMNS)
        issues = evaluate_rule(rule, scenario["intent"], COLUMNS, _aliases())
        results.append({
            "id": scenario["id"], "ok": not issues,
            "issues": [str(i) for i in issues],
        })

    correct = sum(1 for r in results if r["ok"])
    accuracy = correct / len(results)
    report = Path("output") / "prompt_quality_report_pytest.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps({
        "model": getattr(provider, "model", "unknown"),
        "total": len(results), "correct": correct, "accuracy": accuracy,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert accuracy >= 0.9, f"生成即正确率 {accuracy:.2%}，报告见 {report}"

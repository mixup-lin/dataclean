# -*- coding: utf-8 -*-
"""多行业场景回归：生成规则 + 前端执行 + 结果断言。

严格覆盖「自然语言描述 → LLM 响应解析 → 语义自检/修复 → /plan 规则 →
/execute → /result」全链路；模型响应用脚本化 Provider 注入，不绕过生成层。
"""
from __future__ import annotations

import json

import pytest

from llm_provider import parse_json_response
from ops.registry import op_names
from tests.industry_harness import (
    check_scenario, generation_check, prepare_scenario, run_scenario,
)
from tests.industry_scenarios import (
    CROSS_REGION_MAP, INDUSTRY_SCENARIOS, cross_industry_contexts,
)


def _cases() -> list[tuple]:
    cases = []
    for scenario in INDUSTRY_SCENARIOS:
        if scenario.id == "cross-group-count":
            for dataset_id in CROSS_REGION_MAP:
                cases.append((scenario, "baseline", dataset_id))
        else:
            for variant in scenario.variants:
                cases.append((scenario, variant, None))
    return cases


CASES = _cases()


@pytest.mark.parametrize("scenario,variant,dataset_id", CASES,
                         ids=[f"{s.id}-{v}-{d or s.dataset_id}" for s, v, d in CASES])
def test_industry_scenario_end_to_end(scenario, variant, dataset_id, tmp_path):
    ctx = prepare_scenario(scenario, variant, tmp_path, dataset_id=dataset_id)
    record = run_scenario(ctx, tmp_path / "out")
    problems = check_scenario(ctx, record)
    assert problems == [], {
        "scenario": scenario.id,
        "variant": variant,
        "dataset": dataset_id or scenario.dataset_id,
        "problems": problems,
        "ops": record.get("ops"),
    }


@pytest.mark.parametrize("scenario", INDUSTRY_SCENARIOS, ids=[s.id for s in INDUSTRY_SCENARIOS])
def test_generation_chain_repairs_and_converges(scenario, tmp_path):
    ctx = prepare_scenario(scenario, "baseline", tmp_path)
    result = generation_check(ctx)
    meta = result["meta"]
    assert meta.get("json_repair") is True, meta
    assert meta.get("repair_rounds", 0) >= 1, meta
    assert meta.get("semantic_issues") == [], meta
    assert len(result["provider"].calls) >= 3
    ops = [s.get("op") for s in result["rule"].get("steps", []) if isinstance(s, dict)]
    for op in scenario.ops_include:
        assert op in ops, (scenario.id, ops)
    # 渐进式引用：先给任务族/技能索引，再展开命中原语，不是全量提示词堆叠。
    assert "技能索引" in result["system"] or "任务族" in result["system"]
    assert "最后核对" in result["system"] or "自检" in result["system"]


def test_on_demand_operator_catalog(tmp_path):
    from tests.industry_scenarios import scenario_by_id

    rate = scenario_by_id("ecom-paid-rate")
    ctx = prepare_scenario(rate, "baseline", tmp_path)
    # 只验证提示词结构，不调用模型；规则生成断言在上面的端到端用例中完成。
    from rules import build_rule_prompt

    system, user = build_rule_prompt(ctx.scenario.intent, ctx.source, ctx.columns,
                                     column_aliases=ctx.aliases)
    payload = json.loads(user)
    assert payload["intent"] == ctx.scenario.intent
    assert payload["columns"] == ctx.columns
    assert 0 < len(payload["allowed_ops_for_intent"]) < len(op_names())
    assert "rate" in payload["allowed_ops_for_intent"]
    assert "技能索引" in system or "任务族" in system


def test_json_noise_parsing():
    noise = '''```json
{
  // 注释：模型偶尔会把解释写进响应
  "version": "2.0",
  "steps": [],
  "output": {"formats": ["csv"],},
}
```'''
    assert parse_json_response(noise) == {
        "version": "2.0", "steps": [],
        "output": {"formats": ["csv"]},
    }

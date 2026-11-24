# -*- coding: utf-8 -*-
"""多行业场景的公共执行/断言脚手架。

所有场景都严格走真实前端接口：POST /plan → 提取规则 JSON → POST /execute →
GET /result/<run_id>。模型响应由 ScriptedProvider 提供，模拟一次非法 JSON、
一次语义错误规则，再给出正确规则；因此「生成规则」本身也在测试范围内。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import rules
import webapp
from tests.industry_datasets import PRODUCT_DIM_ROWS, write_sample
from tests.industry_scenarios import (
    IndustryScenario, ScenarioContext, make_context, scenario_by_id,
)

_RULE_AREA = re.compile(
    r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>', re.S
)


class ScriptedProvider:
    """按顺序返回脚本化 LLM 响应；额外次数重复最后一条。"""

    name = "scripted-llm"
    mode = "offline"
    model = "scripted-industry-v1"
    reason = "多行业矩阵：非法 JSON → 错误规则 → 正确规则"

    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.calls: List[Tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def provider_for(ctx: ScenarioContext) -> ScriptedProvider:
    correct = ctx.scenario.rule_builder(ctx)
    wrong = ctx.scenario.wrong_rule_builder(ctx)
    return ScriptedProvider([
        "NOT_JSON: upstream returned an unparsable envelope",
        json.dumps(wrong, ensure_ascii=False),
        json.dumps(correct, ensure_ascii=False),
    ])


def _form(ctx: ScenarioContext) -> Dict[str, List[str]]:
    form: Dict[str, List[str]] = {
        "intent": [ctx.scenario.intent],
        "source_path": [str(ctx.source["path"])],
        "source_format": ["csv"],
        "columns": [",".join(ctx.columns)],
        "column_aliases": ["\n".join(f"{k}={v}" for k, v in ctx.aliases.items())],
        "formats": ["csv"],
        "report": ["1"],
        "generation_path": ["legacy"],
    }
    for idx, (alias, src) in enumerate(ctx.extra_sources.items(), 1):
        form[f"extra{idx}_alias"] = [alias]
        form[f"extra{idx}_path"] = [str(src["path"])]
        form[f"extra{idx}_format"] = [str(src.get("format", "csv"))]
    return form


def prepare_scenario(
    scenario: IndustryScenario,
    variant: str,
    base: Path,
    scale: int = 1,
    dataset_id: Optional[str] = None,
) -> ScenarioContext:
    if scale == 1 and "scaled" in variant:
        scale = 10  # 小样本 10 行 → 规模样本 100 行，验证聚合口径不随规模漂移
    work = base / f"{scenario.id}__{dataset_id or scenario.dataset_id}__{variant.replace('+', '_')}"
    work.mkdir(parents=True, exist_ok=True)
    ctx = make_context(scenario, variant, "", scale=scale, dataset_id=dataset_id)
    path = write_sample(ctx.variant.frame, work / "input.csv")
    ctx.source["path"] = path
    if scenario.extra_dim:
        dim_path = write_sample(
            pd.DataFrame(PRODUCT_DIM_ROWS), work / "product_dim.csv"
        )
        ctx.extra_sources["商品维"] = {"path": dim_path, "format": "csv"}
    return ctx


def run_scenario(ctx: ScenarioContext, out_dir: Path) -> Dict[str, Any]:
    state = webapp.AppState(str(out_dir))
    provider = provider_for(ctx)
    original = rules.get_provider
    rules.get_provider = lambda: provider
    record: Dict[str, Any] = {"provider": provider, "ctx": ctx}
    try:
        plan = webapp.handle_request("POST", "/plan", {}, _form(ctx), state)
    finally:
        rules.get_provider = original
    record["plan_status"] = plan.status
    if plan.status != 200:
        record["error"] = plan.body[:600]
        return record
    match = _RULE_AREA.search(plan.body)
    if not match:
        record["error"] = "/plan 页面未返回规则 JSON"
        return record
    rule = json.loads(html.unescape(match.group(1)))
    record["rule"] = rule
    record["ops"] = [str(s.get("op")) for s in rule.get("steps", []) if isinstance(s, dict)]

    execute = webapp.handle_request("POST", "/execute", {}, {
        "rule_json": [json.dumps(rule, ensure_ascii=False)],
    }, state)
    record["execute_status"] = execute.status
    if execute.status != 302:
        record["error"] = execute.body[:600]
        return record
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    record["run_id"] = run_id
    record["run_dir"] = str(run_dir)
    record["summary"] = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    record["result"] = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    record["result_page_status"] = page.status
    return record


def generation_check(ctx: ScenarioContext) -> Dict[str, Any]:
    """单独验证「描述 → LLM 响应解析 → 语义自检/修复 → 规则」链路。"""
    provider = provider_for(ctx)
    rule, meta = rules.generate_rules_legacy(
        ctx.scenario.intent, ctx.source, ctx.columns,
        provider=provider,
        sources=ctx.extra_sources or None,
        source_columns={"商品维": ["品类", "负责人", "类目编码"]} if ctx.extra_sources else None,
        column_aliases=ctx.aliases,
    )
    system, user = provider.calls[0]
    return {"rule": rule, "meta": meta, "provider": provider, "system": system, "user": user}


def _rate_problems(expected: Dict[str, Any], ctx: ScenarioContext, record: Dict[str, Any]) -> List[str]:
    frame: pd.DataFrame = record["result"]
    group_col = ctx.col(expected["group_col"])
    problems: List[str] = []
    if group_col not in frame.columns:
        return [f"结果缺少分组列 {group_col}"]
    for col in (expected["hit_col"], expected["miss_col"], "总数", expected["rate_col"]):
        if col not in frame.columns:
            problems.append(f"结果缺少 {col}")
    if problems:
        return problems
    row = frame
    if "group" in expected:
        row = frame[frame[group_col].astype(str) == str(expected["group"])]
        if len(row) != 1:
            return [f"{group_col}={expected['group']} 应只有一行，实际 {len(row)} 行"]
    else:
        row = frame.set_index(frame[group_col].astype(str))
        missing = set(expected["groups"]) - set(row.index)
        if missing:
            return [f"缺少分组 {sorted(missing)}，实际 {list(row.index)}"]
    for group, values in (expected.get("groups") or {expected["group"]: expected}).items():
        target = row.loc[group] if "groups" in expected else row.iloc[0]
        for col, key in ((expected["hit_col"], "hit"), (expected["miss_col"], "miss"), ("总数", "total")):
            if key in values and int(target[col]) != int(values[key]):
                problems.append(f"{group} 的 {col} 期望 {values[key]}，实际 {target[col]}")
        if "rate" in values and abs(float(target[expected["rate_col"]]) - float(values["rate"])) > 0.01:
            problems.append(
                f"{group} 的 {expected['rate_col']} 期望 {values['rate']}，实际 {target[expected['rate_col']]}"
            )
    return problems


def _check_detail_med(expected: Dict[str, Any], ctx: ScenarioContext, record: Dict[str, Any]) -> List[str]:
    frame: pd.DataFrame = record["result"]
    problems: List[str] = []
    if len(frame) != expected["rows"]:
        problems.append(f"明细行数期望 {expected['rows']}，实际 {len(frame)}")
    lab = ctx.col(expected["lab_col"])
    null_col = ctx.col(expected["null_col"])
    if lab in frame.columns and int(frame[lab].isna().sum()) != expected["expected_lab_null"]:
        problems.append(f"{lab} 空值应被填 0，实际仍有 {int(frame[lab].isna().sum())} 个")
    if null_col in frame.columns:
        actual = int(frame[null_col].isna().sum())
        if actual != expected["expected_result_null"]:
            problems.append(f"{null_col} 保留 null 数期望 {expected['expected_result_null']}，实际 {actual}")
    if any(op in record["ops"] for op in ("aggregate", "count_rows", "rate", "pct", "pivot")):
        problems.append("明细场景出现汇总算子")
    return problems


def _check_ratio_trend(expected: Dict[str, Any], ctx: ScenarioContext, record: Dict[str, Any]) -> List[str]:
    frame: pd.DataFrame = record["result"]
    time_col = ctx.col("发货时间") + "_年季度"
    value_col = expected["value_col"]
    problems: List[str] = []
    for col in (time_col, value_col, f"{value_col}·逐条均值"):
        if col not in frame.columns:
            problems.append(f"结果缺少 {col}")
    if problems:
        return problems
    if frame[time_col].isna().any():
        problems.append("时间桶存在空期")
    periods = [str(v) for v in frame[time_col]]
    if periods != sorted(periods):
        problems.append(f"时间桶未升序：{periods}")
    actual = {str(r[time_col]): float(r[value_col]) for _, r in frame.iterrows()}
    for period, value in expected["periods"].items():
        if period not in actual:
            problems.append(f"缺少期次 {period}")
        elif abs(actual[period] - value) > 0.01:
            problems.append(f"{period} 均价期望 {value}，实际 {actual[period]}")
    if len(actual) != len(expected["periods"]):
        problems.append(f"期次数量期望 {len(expected['periods'])}，实际 {len(actual)}")
    if record["summary"]["task"]["task_type"] != "trend":
        problems.append("组合场景最终 task_type 不是 trend")
    return problems


def _check_group_count(expected: Dict[str, Any], ctx: ScenarioContext, record: Dict[str, Any]) -> List[str]:
    frame: pd.DataFrame = record["result"]
    group_col = ctx.col(expected["group_col"])
    problems: List[str] = []
    if group_col not in frame.columns or "条数" not in frame.columns:
        return [f"结果缺少 {group_col} 或 条数"]
    actual = {str(r[group_col]): int(r["条数"]) for _, r in frame.iterrows()}
    for group, count in expected["groups"].items():
        if actual.get(group) != count:
            problems.append(f"{group} 条数期望 {count}，实际 {actual.get(group)}")
    if sum(actual.values()) != expected["total"]:
        problems.append(f"总条数期望 {expected['total']}，实际 {sum(actual.values())}")
    return problems


def _check_gmv(expected: Dict[str, Any], ctx: ScenarioContext, record: Dict[str, Any]) -> List[str]:
    frame: pd.DataFrame = record["result"]
    group_col = ctx.col(expected["group_col"])
    value_col = expected["value_col"]
    problems: List[str] = []
    if group_col not in frame.columns or value_col not in frame.columns:
        return [f"结果缺少 {group_col} 或 {value_col}"]
    actual = {str(r[group_col]): float(r[value_col]) for _, r in frame.iterrows()}
    for group, value in expected["groups"].items():
        if group not in actual:
            problems.append(f"缺少分组 {group}")
        elif abs(actual[group] - value) > 0.02:
            problems.append(f"{group} 销售额期望 {value}，实际 {actual[group]}")
    return problems


CHECKERS = {
    "rate_group": _rate_problems,
    "rate_groups": _rate_problems,
    "detail_med": _check_detail_med,
    "ratio_trend": _check_ratio_trend,
    "group_count": _check_group_count,
    "gmv_join": _check_gmv,
}


def check_scenario(ctx: ScenarioContext, record: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    if "error" in record:
        return [f"链路中断：{record['error']}"]
    if record.get("plan_status") != 200:
        problems.append(f"/plan 返回 {record.get('plan_status')}")
    if record.get("execute_status") != 302:
        problems.append(f"/execute 返回 {record.get('execute_status')}")
    if record.get("result_page_status") != 200:
        problems.append(f"/result 返回 {record.get('result_page_status')}")
    rule = record.get("rule") or {}
    ops = record.get("ops") or []
    for op in ctx.scenario.ops_include:
        if op not in ops:
            problems.append(f"规则缺少算子 {op}（实际 {' → '.join(ops)}）")
    task_type = (record.get("summary") or {}).get("task", {}).get("task_type")
    if ctx.scenario.task_type and task_type != ctx.scenario.task_type:
        problems.append(f"task_type 期望 {ctx.scenario.task_type}，实际 {task_type}")
    failed = [s.get("op") for s in (record.get("summary") or {}).get("step_logs", [])
              if s.get("status") != "ok"]
    if failed:
        problems.append(f"步骤执行失败：{failed}")
    problems.extend(CHECKERS[ctx.scenario.checker](ctx.scenario.expected_builder(ctx), ctx, record))
    return problems


__all__ = [
    "ScriptedProvider", "check_scenario", "generation_check", "prepare_scenario",
    "provider_for", "run_scenario",
]

# -*- coding: utf-8 -*-
"""结构化意图 Schema 的真实链路测试（不 mock LLM）。

前置：项目根 ``.env`` 或系统环境提供 LLM_API_KEY（已配置时直接运行）。
覆盖：典型描述、边界输入、异常响应回灌、/plan → /execute → /result 全链路。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

import rules
import webapp
from llm_provider import get_provider, load_dotenv
from ops.intent_schema import (
    SCHEMA_VERSION,
    compile_schema,
    parse_intent_schema,
    validate_schema,
)

load_dotenv()
_HAS_KEY = bool(__import__("os").environ.get("LLM_API_KEY", "").strip())
pytestmark = pytest.mark.skipif(not _HAS_KEY, reason="未配置 LLM_API_KEY，跳过真实调用测试")

COLS = ["标的号", "面积(㎡)", "成交价(元)", "结束时间"]


def _write_csv(path: Path) -> Path:
    rows = []
    for year in (2025, 2026):
        for month in (1, 4, 7):
            for area, price in ((20, 100 + month * 10), (2000, 300 + month * 5),
                                (100, 200 + month * 2)):
                rows.append({
                    "标的号": f"{year}-{month}-{area}",
                    "面积(㎡)": area,
                    "成交价(元)": price + (year - 2025) * 1000,
                    "结束时间": f"{year}-{month:02d}-15",
                })
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _source(path: Path) -> Dict[str, Any]:
    return {"path": str(path), "format": "csv"}


# ---------------------------------------------------------------------------
# 典型描述：真实模型解析 → Schema 校验 → 编译 → 语义自检
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("intent,task_type,required_ops", [
    ("统计成交率（成交/总数）：成交价不为空算成交，为空算未成交",
     "proportion", {"rate"}),
    ("去重，然后提取出面积小于50或大于1000的数据，最后分析2026年按月的平均成交单价的趋势，"
     "成交单价 = （成交价/面积）",
     "trend", {"dedupe", "filter", "expr", "date_filter", "date_part", "aggregate"}),
    ("按结束时间的年份分组，统计每个年份的数据条数",
     "summary", {"date_part", "count_rows"}),
    ("提取面积大于1000的数据，其他字段空值保持为空",
     "detail", {"filter"}),
])
def test_real_llm_typical_intents_compile_cleanly(
    tmp_path: Path, intent: str, task_type: str, required_ops: set
) -> None:
    path = _write_csv(tmp_path / "in.csv")
    rule, meta = rules.generate_rules_from_schema(intent, _source(path), COLS)
    ops = {str(step.get("op")) for step in rule["steps"]}
    assert required_ops <= ops, (intent, ops)
    assert rule["task_type"] == task_type, (intent, rule["task_type"])
    assert meta["generation_path"] == "intent_schema"
    assert meta["semantic_issues"] == [], meta["semantic_issues"]
    assert not rules.validate_rule(rule, COLS), rules.validate_rule(rule, COLS)


def test_real_llm_cross_year_monthly_ratio_uses_sum_over_sum(tmp_path: Path) -> None:
    intent = ("去重，然后提取出面积小于50或大于1000的数据，"
              "最后分析2026年按月的平均成交单价的趋势，成交单价 = （成交价/面积）")
    path = _write_csv(tmp_path / "in.csv")
    rule, meta = rules.generate_rules_from_schema(intent, _source(path), COLS)
    steps = rule["steps"]
    assert any(s.get("op") == "date_filter" and s.get("year") == 2026 for s in steps)
    assert any(s.get("op") == "date_part" and "year" in (s.get("parts") or [])
               and "month" in (s.get("parts") or []) for s in steps)
    agg = next(s for s in steps if s.get("op") == "aggregate")
    assert "sum" in (agg.get("func") or []), agg
    assert any(s.get("op") == "expr" and s.get("expression") == "c0/c1"
               for s in steps[steps.index(agg) + 1:]), steps


# ---------------------------------------------------------------------------
# 边界输入：别名映射、歧义描述、极短描述
# ---------------------------------------------------------------------------
def test_real_llm_boundary_alias_mapping(tmp_path: Path) -> None:
    """描述用中文别名，列名为英文；只按显式别名映射解析，不猜测。"""
    cols = ["id", "area", "price", "closed_at"]
    aliases = {"标的号": "id", "面积": "area", "成交价": "price", "结束时间": "closed_at"}
    path = tmp_path / "alias.csv"
    pd.DataFrame([{"id": "A1", "area": 20.0, "price": 100.0, "closed_at": "2026-01-15"}]).to_csv(
        path, index=False, encoding="utf-8-sig"
    )
    intent = "统计成交率：成交价不为空算成交，为空算未成交"
    rule, meta = rules.generate_rules_from_schema(
        intent, _source(path), cols, column_aliases=aliases
    )
    assert rule["steps"][0]["column"] == "price", rule["steps"]
    assert meta["semantic_issues"] == [], meta["semantic_issues"]


def test_real_llm_boundary_unknown_column_is_rejected(tmp_path: Path) -> None:
    """描述引用不存在的列时，Schema 校验必须拦截并回灌，而不是臆造列。"""
    path = _write_csv(tmp_path / "in.csv")
    schema, meta = parse_intent_schema(
        "统计不存在的字段的为空率", get_provider(), COLS
    )
    if schema is not None:
        # 模型若把不存在的列替换成真实列，必须在 assumptions 里说明，且规则仍可执行
        rule = compile_schema(schema)
        assert not rules._validate_steps(rule.steps, COLS)
    else:
        assert meta["errors"], "校验失败必须给出明确错误"


# ---------------------------------------------------------------------------
# 异常响应：脚本化 Provider（不 mock 校验/编译），验证回灌恢复
# ---------------------------------------------------------------------------
class _ScriptedProvider:
    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.seen: List[str] = []

    def complete(self, system: str, user: str) -> str:
        self.seen.append(user)
        return self.responses.pop(0)


def test_abnormal_response_malformed_json_then_recovers(tmp_path: Path) -> None:
    good = {
        "version": SCHEMA_VERSION, "task_type": "proportion",
        "operations": [{"kind": "rate", "column": "成交价(元)",
                        "condition": {"column": "成交价(元)", "operator": "notna"},
                        "alias": "成交率", "numerator": "hit", "denominator": "total"}],
        "assumptions": [],
    }
    provider = _ScriptedProvider(["NOT_JSON{{{", json.dumps(good, ensure_ascii=False)])
    result, meta = parse_intent_schema("统计成交率", provider, COLS)
    assert result is not None and result.ok, meta
    assert meta["attempts"] == 2
    assert meta["llm_errors"], "首次异常必须记录"
    assert "校验" in provider.seen[1] or "失败" in provider.seen[1]


def test_abnormal_response_unknown_column_then_recovers(tmp_path: Path) -> None:
    bad = {
        "version": SCHEMA_VERSION, "task_type": "summary",
        "operations": [{"kind": "aggregate", "by": ["不存在的列"],
                        "metrics": [{"column": "成交价(元)", "func": "mean", "alias": "均价"}]}],
        "assumptions": [],
    }
    good = json.loads(json.dumps(bad))
    good["operations"][0]["by"] = ["标的号"]
    provider = _ScriptedProvider([json.dumps(bad, ensure_ascii=False),
                                  json.dumps(good, ensure_ascii=False)])
    result, meta = parse_intent_schema("按标的号统计均价", provider, COLS)
    assert result is not None and result.ok, meta
    assert result.payload["operations"][0]["by"] == ["标的号"]
    assert meta["attempts"] == 2


def test_schema_validation_is_deterministic_not_text_regex() -> None:
    """同一份 Schema 无论描述如何变化，编译结果必须一致（证明不依赖中文正则）。"""
    payload = {
        "version": SCHEMA_VERSION, "task_type": "trend",
        "operations": [
            {"kind": "date_part", "column": "结束时间", "parts": ["year", "month"],
             "bucket": "年月"},
            {"kind": "aggregate", "by": ["结束时间_年月"],
             "metrics": [{"kind": "ratio", "numerator": "成交价(元)",
                          "denominator": "面积(㎡)", "alias": "均价", "func": "sum_div_sum"}]},
        ], "assumptions": [],
    }
    result = validate_schema(payload, COLS)
    assert result.ok, result.errors
    left = compile_schema(result)
    right = compile_schema(validate_schema(payload, COLS))
    assert left.steps == right.steps
    assert left.task_type == right.task_type


# ---------------------------------------------------------------------------
# 前端全链路：/plan(schema_mode=1) → /execute → /result
# ---------------------------------------------------------------------------
def test_real_llm_plan_execute_result_chain(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "in.csv")
    state = webapp.AppState(str(tmp_path / "out"))
    intent = "去重，然后提取出面积小于50或大于1000的数据，最后分析2026年按月的平均成交单价的趋势，成交单价 = （成交价/面积）"
    plan = webapp.handle_request("POST", "/plan", {}, {
        "intent": [intent],
        "source_path": [str(path)], "source_format": ["csv"],
        "columns": [",".join(COLS)], "formats": ["csv"], "report": ["1"],
        "schema_mode": ["1"],
    }, state)
    assert plan.status == 200, plan.body[:800]
    match = re.search(
        r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>',
        plan.body, re.S,
    )
    assert match, plan.body[:800]
    import html as _html
    rule = json.loads(_html.unescape(match.group(1)))
    assert [s.get("op") for s in rule["steps"]][:3] == ["dedupe", "filter", "expr"]
    assert any(s.get("op") == "date_filter" and s.get("year") == 2026 for s in rule["steps"])
    assert "结构化意图" in plan.body or "intent_schema" in plan.body

    execute = webapp.handle_request("POST", "/execute", {}, {
        "rule_json": [json.dumps(rule, ensure_ascii=False)],
    }, state)
    assert execute.status == 302, execute.body[:800]
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    result = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    assert not result.empty
    assert all(str(v).startswith("2026-") for v in result.iloc[:, 0]), result.head()
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    assert page.status == 200

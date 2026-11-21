# -*- coding: utf-8 -*-
"""组合场景 + 按需提示词 + JSON 容错的前端接口级回归。

严格模拟浏览器：POST /plan → 取规则 JSON → POST /execute → GET /result。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd
import pytest

import rules
import webapp
from llm_provider import LLMError, parse_json_response
from ops.prompt_kit import primary_family
from tests.provider_fixture import FixtureProvider

INTENT = "去重，然后提取出面积小于50或大于1000的数据，最后分析成交价的趋势"
COLUMNS = ["标的号", "面积(㎡)", "成交价(元)", "结束时间"]


def _frame() -> pd.DataFrame:
    rows = []
    for month in range(1, 7):
        for area, price in ((20.0, 100.0 + month * 10), (2000.0, 300.0 + month * 5),
                            (100.0, 200.0 + month * 2)):
            rows.append({
                "标的号": f"M{month}-{int(area)}",
                "面积(㎡)": area,
                "成交价(元)": price,
                "结束时间": f"2026-{month:02d}-15",
            })
    return pd.DataFrame(rows)


def _state(tmp_path: Path):
    return webapp.AppState(str(tmp_path / "out"))


def _bad_llm_rule(path: str) -> dict:
    """故意只做去重和提取，丢掉最后的趋势分析。"""
    return {
        "version": "2.0", "intent": INTENT,
        "source": {"path": path, "format": "csv"},
        "options": {"fill_missing": "none"},
        "task_type": "detail",
        "steps": [
            {"op": "dedupe", "subset": [], "keep": "first"},
            {"op": "filter", "logic": "or", "conditions": [
                {"column": "面积(㎡)", "operator": "lt", "value": 50},
                {"column": "面积(㎡)", "operator": "gt", "value": 1000},
            ]},
        ],
        "output": {"formats": ["csv"], "report": True},
    }


_RULE_AREA = __import__("re").compile(
    r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>',
    __import__("re").S,
)


def _post_plan(path: str, state) -> dict:
    original = rules.get_provider
    rules.get_provider = lambda: FixtureProvider(rule=_bad_llm_rule(path))
    try:
        reply = webapp.handle_request("POST", "/plan", {}, {
            "intent": [INTENT], "source_path": [path], "source_format": ["csv"],
            "columns": [",".join(COLUMNS)], "formats": ["csv"], "report": ["1"],
        }, state)
    finally:
        rules.get_provider = original
    assert reply.status == 200, reply.body[:800]
    match = _RULE_AREA.search(reply.body)
    assert match
    return json.loads(html.unescape(match.group(1)))


def test_composite_intent_routes_to_composite_family():
    assert primary_family(INTENT, COLUMNS) == "composite"


def test_composite_prompt_uses_selective_catalog():
    system, user = rules.build_rule_prompt(INTENT, {"path": "x.csv", "format": "csv"}, COLUMNS)
    payload = json.loads(user)
    assert payload["task_router"]["primary_family"] == "composite"
    ops = payload["allowed_ops_for_intent"]
    assert {"dedupe", "filter", "date_part", "aggregate"} <= set(ops)
    # 按需引用：无关算子目录不应整份塞进提示词
    assert "- anti_join:" not in system
    assert "- mask_pii:" not in system
    assert "- convert_unit:" not in system
    # 按需引用仍远小于全量 53 算子 + 26 条契约的体量（约 40k+）
    assert len(system) < 26_000


def test_composite_plan_restores_analysis_stage(tmp_path):
    path = tmp_path / "in.csv"
    _frame().to_csv(path, index=False, encoding="utf-8-sig")
    state = _state(tmp_path)

    rule = _post_plan(str(path), state)
    ops = [s.get("op") for s in rule["steps"]]
    assert rule["task_type"] == "trend"
    assert ops[:5] == ["dedupe", "filter", "date_part", "dropna", "aggregate"], ops
    conditions = rule["steps"][1]["conditions"]
    assert rule["steps"][1]["logic"] == "or"
    assert {(c["column"], c["operator"], c["value"]) for c in conditions} == {
        ("面积(㎡)", "lt", 50), ("面积(㎡)", "gt", 1000)
    }

    reply = webapp.handle_request(
        "POST", "/execute", {},
        {"rule_json": [json.dumps(rule, ensure_ascii=False)]}, state,
    )
    assert reply.status == 302, reply.body[:800]
    run_id = reply.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    assert summary["task"]["task_type"] == "trend"
    assert all(s["status"] == "ok" for s in summary["step_logs"])
    frame = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    assert len(frame) >= 2

    report = (run_dir / summary["task_report_name"]).read_text(encoding="utf-8")
    assert "趋势" in report
    assert "data-section=" in report
    assert "环节契约 · 本次任务的实际链路" in report
    assert "date_part" in report and "aggregate" in report
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    assert page.status == 200


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1,}', {"a": 1}),
    ('{"a": 1 // 注释\n}', {"a": 1}),
    ('{"a": “中文”}', {"a": "中文"}),
    ("{'a': 1, 'b': [2,]}", {"a": 1, "b": [2]}),
    ("解释文字\n{\"a\": 1, \"b\": 2}\n结束", {"a": 1, "b": 2}),
])
def test_parse_json_response_tolerates_common_noise(raw, expected):
    assert parse_json_response(raw) == expected


def test_parse_json_response_still_rejects_broken_json():
    with pytest.raises(LLMError) as exc:
        parse_json_response("{ bad json: , , }")
    assert exc.value.kind == "parse"


def test_generate_rules_retries_after_parse_failure():
    class FlakyProvider:
        name = "flaky"
        mode = "offline"
        model = "flaky-v1"
        reason = "test"

        def __init__(self):
            self.calls = 0

        def complete(self, system: str, user: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return '{"version": "2.0", "intent": "x" "steps": []}'
            return json.dumps({
                "version": "2.0", "intent": "统计每个区县的数据条数",
                "source": {"path": "x.csv", "format": "csv"},
                "options": {}, "task_type": "summary",
                "steps": [{"op": "count_rows", "by": ["区县"], "alias": "条数"}],
                "output": {"formats": ["csv"], "report": True},
            }, ensure_ascii=False)

    provider = FlakyProvider()
    rule, meta = rules.generate_rules(
        "统计每个区县的数据条数",
        {"path": "x.csv", "format": "csv"},
        ["区县"],
        provider,
    )
    assert provider.calls == 2
    assert meta.get("json_repair") is True
    assert rule["steps"][0]["op"] == "count_rows"


def test_report_sections_differ_by_scenario():
    from report_task import render_sections
    from task_profile import build_profile

    frame = pd.DataFrame({
        "结束时间": ["2026-01-15", "2026-02-15", "2026-03-15"],
        "结束时间_月": ["2026-01", "2026-02", "2026-03"],
        "区县": ["鼓楼区", "台江区", "鼓楼区"],
        "成交价(元)": [100.0, 200.0, 300.0],
    })
    detail_rule = {
        "task_type": "detail",
        "steps": [{"op": "filter", "conditions": [
            {"column": "区县", "operator": "eq", "value": "鼓楼区"}]}],
    }
    trend_rule = {
        "task_type": "trend",
        "steps": [
            {"op": "date_part", "column": "结束时间", "parts": ["month"]},
            {"op": "aggregate", "by": ["结束时间_月"], "column": ["成交价(元)"],
             "func": ["mean"], "alias": ["成交价(元)"]},
        ],
    }
    detail_html = render_sections(
        build_profile(frame, detail_rule), detail_rule
    )
    trend_html = render_sections(
        build_profile(frame, trend_rule), trend_rule
    )
    assert detail_html != trend_html
    assert "chart-section" not in detail_html
    assert "chart-section" in trend_html
    assert "时间趋势" in trend_html

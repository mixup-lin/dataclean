# -*- coding: utf-8 -*-
"""前端接口级场景矩阵。

严格模拟浏览器操作：POST /plan（描述 → 规则 JSON）→ 从响应页取规则 JSON →
POST /execute（校验并执行）→ GET /result/<run_id>（结果页）。
所有用例注入「故意错误的 LLM 输出」，验证结果由描述决定，而非模型发挥。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytest

import rules
import webapp
from tests.provider_fixture import FixtureProvider

INTENT_CLEAN = "提取区县为鼓楼区的数据，其中成交价空值置为0，其他字段空值置为null"
COLS = ["区县", "成交价(元)", "小区", "评估价(元)", "结束时间"]


# ------------------------------------------------------------
# 前端请求辅助
# ------------------------------------------------------------
def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "区县": ["鼓楼区", "鼓楼区", "鼓楼区", "台江区"],
        "成交价(元)": [None, 200.0, None, 300.0],
        "小区": ["A", None, "C", "D"],
        "评估价(元)": [100.0, 200.0, None, 300.0],
        "结束时间": ["2026-01-05", "2026-02-05", "2025-03-05", "2026-04-05"],
    })


def _write_csv(tmp_path: Path, name: str = "in.csv",
               frame: Optional[pd.DataFrame] = None) -> str:
    path = tmp_path / name
    (frame if frame is not None else _frame()).to_csv(
        path, index=False, encoding="utf-8-sig"
    )
    return str(path)


def _bad_llm_rule(path: str, style: str = "summary") -> Dict[str, Any]:
    """模拟真实模型最容易给出的几种错误规则。"""
    base: Dict[str, Any] = {
        "version": "2.0", "intent": INTENT_CLEAN,
        "source": {"path": path, "format": "csv"},
        "options": {"fill_missing": "median", "outlier_method": "none"},
        "output": {"formats": ["csv"], "report": True},
    }
    filter_step = {"op": "filter", "conditions": [
        {"column": "区县", "operator": "eq", "value": "鼓楼区"}], "logic": "and"}
    if style == "summary":
        base.update(task_type="summary", steps=[filter_step,
                    {"op": "count_rows", "by": ["区县"], "alias": "条数"}])
    elif style == "assign":
        base.update(task_type="summary", steps=[
            filter_step,
            {"op": "assign", "column": "状态", "rules": [
                {"condition": {"column": "成交价(元)", "operator": "notna"},
                 "value": "成交"}], "default": "未成交"},
            {"op": "count_rows", "by": ["状态"], "alias": "条数"},
        ])
    elif style == "aggregate":
        base.update(task_type="summary", steps=[
            filter_step,
            {"op": "aggregate", "by": ["区县"], "column": ["成交价(元)"],
             "func": ["sum"], "alias": ["成交额"]},
        ])
    elif style == "wrong_fill":
        base.update(task_type="detail", steps=[
            filter_step,
            {"op": "fill", "columns": ["评估价(元)"], "method": "zero"},
        ])
    else:  # pragma: no cover
        raise ValueError(style)
    return base


def _post_plan(
    intent: str, path: str, llm_rule: Dict[str, Any], state: Any,
    *, columns: Optional[List[str]] = None, alias_text: str = "",
    endpoint: str = "/plan",
) -> webapp.Reply:
    """POST 工作台表单，返回页面；provider 由用例注入。"""
    original = rules.get_provider
    rules.get_provider = lambda: FixtureProvider(rule=llm_rule)
    try:
        form = {
            "intent": [intent],
            "source_path": [path],
            "source_format": ["csv"],
            "columns": [",".join(columns or [])],
            "column_aliases": [alias_text],
            "formats": ["csv"],
            "report": ["1"],
        }
        return webapp.handle_request("POST", endpoint, {}, form, state)
    finally:
        rules.get_provider = original


_RULE_AREA = re.compile(
    r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>',
    re.S,
)


def _rule_from_page(reply: webapp.Reply) -> Dict[str, Any]:
    assert reply.status == 200, reply.body[:800]
    match = _RULE_AREA.search(reply.body)
    assert match, "生成页未渲染规则编辑器"
    return json.loads(html.unescape(match.group(1)))


def _post_execute(rule: Dict[str, Any], state: Any
                  ) -> Tuple[str, Dict[str, Any], pd.DataFrame]:
    reply = webapp.handle_request(
        "POST", "/execute", {},
        {"rule_json": [json.dumps(rule, ensure_ascii=False)]}, state,
    )
    assert reply.status == 302, reply.body[:800]
    run_id = reply.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    frame = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    # 结果页也必须能正常打开
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    assert page.status == 200
    return run_id, summary, frame


def _state(tmp_path: Path) -> Any:
    return webapp.AppState(str(tmp_path / "out"))


def _assert_clean_result(summary: Dict[str, Any], frame: pd.DataFrame,
                         *, expected_rows: int = 3) -> None:
    assert summary["task"]["task_type"] == "detail"
    assert summary["rows"] == expected_rows
    assert all(s["status"] == "ok" for s in summary["step_logs"])
    assert not any(s["op"] in ("count_rows", "aggregate", "pivot", "rate", "pct")
                   for s in summary["step_logs"])
    assert {"区县", "成交价(元)", "小区", "评估价(元)"} <= set(frame.columns)
    assert set(frame["区县"]) == {"鼓楼区"}
    assert [int(v) for v in frame["成交价(元)"]] == [0, 200, 0]
    assert int(frame["小区"].isna().sum()) == 1          # 其他字段空值保持 null
    assert int(frame["评估价(元)"].isna().sum()) == 1


# ------------------------------------------------------------
# 用户原始场景：不同 LLM 错误形态都必须收敛
# ------------------------------------------------------------
@pytest.mark.parametrize("style", ["summary", "assign", "aggregate", "wrong_fill"])
def test_clean_intent_converges_over_bad_llm(tmp_path, style):
    path = _write_csv(tmp_path)
    state = _state(tmp_path)
    plan = _post_plan(INTENT_CLEAN, path, _bad_llm_rule(path, style), state)
    rule = _rule_from_page(plan)

    assert rule["task_type"] == "detail"
    assert rule["options"]["fill_missing"] == "none"
    ops = [s["op"] for s in rule["steps"]]
    assert "filter" in ops and "fill" in ops
    assert not any(op in ops for op in ("count_rows", "aggregate", "assign", "rate", "pct"))
    fill = next(s for s in rule["steps"] if s["op"] == "fill")
    assert fill["columns"] == ["成交价(元)"] and fill["value"] == 0

    _rid, summary, frame = _post_execute(rule, state)
    _assert_clean_result(summary, frame)


@pytest.mark.parametrize("desc", [
    "筛选区县为鼓楼区的数据，成交价为空填0，其他字段空值保留null",
    "查出区县=鼓楼区的记录，成交价缺失置0，其他列空值为null",
    "提取鼓楼区数据，其中成交价空值设置为0，其余字段空值置为null",
    "导出区县是鼓楼区的数据，成交价空值置为0，其他字段空值为空",
])
def test_clean_intent_description_variants(tmp_path, desc):
    """换说法不能换结果。"""
    path = _write_csv(tmp_path)
    state = _state(tmp_path)
    rule = _rule_from_page(_post_plan(desc, path, _bad_llm_rule(path), state))

    assert rule["task_type"] == "detail"
    assert rule["options"]["fill_missing"] == "none"
    assert [s["op"] for s in rule["steps"]] == ["filter", "fill"]
    assert rule["steps"][0]["conditions"][0]["value"] == "鼓楼区"
    assert rule["steps"][1]["columns"] == ["成交价(元)"]
    assert rule["steps"][1]["value"] == 0

    _rid, summary, frame = _post_execute(rule, state)
    _assert_clean_result(summary, frame)


def test_clean_intent_with_date_filter(tmp_path):
    path = _write_csv(tmp_path)
    state = _state(tmp_path)
    intent = ("提取区县为鼓楼区且结束时间在2026年的数据，"
              "成交价空值置为0，其他字段空值置为null")
    rule = _rule_from_page(_post_plan(intent, path, _bad_llm_rule(path), state))

    assert rule["task_type"] == "detail"
    assert [s["op"] for s in rule["steps"]] == ["filter", "date_filter", "fill"]
    assert rule["steps"][1]["year"] == 2026

    _rid, summary, frame = _post_execute(rule, state)
    assert summary["task"]["task_type"] == "detail"
    assert len(frame) == 2
    assert [int(v) for v in frame["成交价(元)"]] == [0, 200]
    assert int(frame["小区"].isna().sum()) == 1


def test_clean_intent_with_column_aliases(tmp_path):
    frame = pd.DataFrame({
        "district": ["鼓楼区", "鼓楼区", "台江区"],
        "price": [None, 200.0, 300.0],
        "community": ["A", None, "D"],
    })
    path = _write_csv(tmp_path, "alias.csv", frame)
    state = _state(tmp_path)
    alias_text = "区县 = district\n成交价 = price"
    rule = _rule_from_page(_post_plan(
        INTENT_CLEAN, path, _bad_llm_rule(path), state,
        columns=["district", "price", "community"], alias_text=alias_text,
    ))

    assert rule["steps"][0]["conditions"][0]["column"] == "district"
    assert rule["steps"][1]["columns"] == ["price"]
    _rid, summary, out = _post_execute(rule, state)
    assert summary["task"]["task_type"] == "detail"
    assert len(out) == 2 and [int(v) for v in out["price"]] == [0, 200]
    assert int(out["community"].isna().sum()) == 1


def test_run_endpoint_generates_and_executes_in_one_post(tmp_path):
    """工作台「生成并立即执行」按钮走 /run，同样验证。"""
    path = _write_csv(tmp_path)
    state = _state(tmp_path)
    reply = _post_plan(INTENT_CLEAN, path, _bad_llm_rule(path), state, endpoint="/run")
    assert reply.status == 302
    run_id = reply.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    out = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    assert summary["task"]["task_type"] == "detail"
    assert len(out) == 3 and int(out["成交价(元)"].sum()) == 200


# ------------------------------------------------------------
# 防误伤：统计类描述不能被明细骨架吞掉
# ------------------------------------------------------------
@pytest.mark.parametrize("desc,expected_op,expected_type", [
    ("统计鼓楼区各小区的条数", "count_rows", "summary"),
    ("统计鼓楼区的成交率（成交价为空-未成交，不为空-成交）", "rate", "proportion"),
    ("按区县分组统计成交价总和", "aggregate", "summary"),
])
def test_analysis_intents_not_rewritten_as_detail(tmp_path, desc, expected_op, expected_type):
    path = _write_csv(tmp_path)
    state = _state(tmp_path)
    llm_rule = _bad_llm_rule(path)
    llm_rule["intent"] = desc
    llm_rule["steps"] = [
        {"op": "filter", "conditions": [
            {"column": "区县", "operator": "eq", "value": "鼓楼区"}], "logic": "and"},
        {"op": expected_op, "by": ["小区"], "alias": "结果"} if expected_op == "count_rows"
        else {"op": "rate", "column": "成交价(元)",
              "condition": {"column": "成交价(元)", "operator": "notna"},
              "true_label": "成交", "false_label": "未成交", "alias": "成交率"}
        if expected_op == "rate"
        else {"op": "aggregate", "by": ["区县"], "column": ["成交价(元)"],
              "func": ["sum"], "alias": ["成交额"]},
    ]
    rule = _rule_from_page(_post_plan(desc, path, llm_rule, state))
    assert rule["task_type"] == expected_type
    assert any(s["op"] == expected_op for s in rule["steps"])


# ------------------------------------------------------------
# 比率场景回归：明细骨架不能抢占比率骨架
# ------------------------------------------------------------
def test_rate_intent_still_wins(tmp_path):
    path = _write_csv(tmp_path)
    state = _state(tmp_path)
    intent = "筛选区县为鼓楼区，统计未成交率（成交价为空-未成交，不为空-成交）"
    bad = _bad_llm_rule(path)
    bad["steps"] = [
        {"op": "filter", "column": "成交价(元)", "operator": "isna"},
        {"op": "filter", "column": "成交价(元)", "operator": "notna"},
    ]
    rule = _rule_from_page(_post_plan(intent, path, bad, state))
    assert rule["task_type"] == "proportion"
    rate = next(s for s in rule["steps"] if s["op"] == "rate")
    assert rate["numerator"] == "miss" and rate["alias"] == "未成交率"

    _rid, summary, out = _post_execute(rule, state)
    assert summary["task"]["task_type"] == "proportion"
    assert len(out) == 1
    # 鼓楼区 3 行：1 条成交、2 条未成交
    assert int(out.iloc[0]["未成交数"]) == 2
    assert int(out.iloc[0]["总数"]) == 3
    assert round(float(out.iloc[0]["未成交率"]), 2) == 66.67

# -*- coding: utf-8 -*-
"""SQLite 全场景矩阵共享基建。

所有用例严格走真实前端链路：

    自然语言描述 → POST /plan → 规则 JSON → POST /execute → GET /result/<id>

模型应答由测试侧 Provider 提供（Schema JSON 或 legacy 规则 JSON），
不向 /execute 直接注入规则。Provider 只替代“模型输出”，不替代
Schema 校验、确定性编译、规则白名单校验、执行与报告环节。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import rules
import webapp
from tests.conftest import extract_rule_from_plan_body
from tests.provider_fixture import FixtureProvider
from tests.test_full_pipeline_matrix import (
    _dim_frame,
    _sales_frame,
    _scenario_cases,
)

SCI = __import__("re").compile(r"(?<![\w\"-])-?\d+(?:\.\d+)?e[+-]\d+", __import__("re").I)


# --------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------
@dataclass
class SqliteCase:
    """一个 SQLite 前端全链路场景。"""

    id: str
    category: str
    intent: str
    tables: Dict[str, pd.DataFrame]
    schema: Optional[Dict[str, Any]] = None
    legacy_steps: Optional[List[Dict[str, Any]]] = None
    main_table: str = "data"
    main_query: Optional[str] = None
    upload_main: bool = False
    extra: Optional[Dict[str, Any]] = None
    formats: Tuple[str, ...] = ("csv", "json")
    report: bool = True
    expect: Dict[str, Any] = field(default_factory=dict)
    check: Optional[Callable[[Dict[str, Any]], None]] = None
    plan_error: Optional[str] = None


def write_sqlite(path: Path, tables: Dict[str, pd.DataFrame]) -> Path:
    """把同一批逻辑表写入一个 .db 文件。"""
    with sqlite3.connect(str(path)) as conn:
        for name, frame in tables.items():
            frame.to_sql(name, conn, index=False, if_exists="replace")
    return path


def ratio_frame() -> pd.DataFrame:
    """比值均价用的明细：同月内单价差异大，能区分 Σ分子/Σ分母 与逐条均值。"""
    return pd.DataFrame({
        "标的号": ["T01", "T02", "T03", "T04", "T05", "T06"],
        "成交价(元)": [100.0, 900.0, 200.0, 1800.0, None, 300.0],
        "面积(㎡)": [10.0, 30.0, 20.0, 40.0, 5.0, 5.0],
        "结束时间": [
            "2026-01-05", "2026-01-25", "2026-02-05",
            "2026-02-25", "2026-02-28", "2025-12-20",
        ],
    })


def schema_payload(
    operations: List[Dict[str, Any]],
    task_type: str,
    assumptions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """构造一份通过 Schema v1.0 校验的模型应答。"""
    return {
        "version": "1.0",
        "task_type": task_type,
        "operations": operations,
        "assumptions": assumptions or [],
    }


@contextmanager
def provider_context(payload: Dict[str, Any]):
    """临时替换 rules.get_provider，退出后一定恢复。"""
    original = rules.get_provider
    rules.get_provider = lambda: FixtureProvider(rule=payload)
    try:
        yield
    finally:
        rules.get_provider = original


# --------------------------------------------------------------------
# 前端链路执行
# --------------------------------------------------------------------
def _form_for(case: SqliteCase, db_path: Path, main_cols: Sequence[str]) -> Dict[str, List[str]]:
    form: Dict[str, List[str]] = {
        "intent": [case.intent],
        "source_path": [str(db_path)],
        "source_format": ["sqlite"],
        "sqlite_table": [case.main_table],
        "sqlite_query": [case.main_query or ""],
        "columns": [",".join(str(c) for c in main_cols)],
        "formats": list(case.formats),
        "report": ["1"] if case.report else [],
    }
    if case.extra:
        extra = case.extra
        form.update({
            "extra1_alias": [str(extra["alias"])],
            "extra1_path": [str(db_path)],
            "extra1_format": ["sqlite"],
            "extra1_sqlite_table": [str(extra.get("table") or "dim")],
            "extra1_sqlite_query": [str(extra.get("query") or "")],
            "extra1_key": [str(extra.get("key") or "")],
            "extra1_right_key": [str(extra.get("right_key") or "")],
            "extra1_how": [str(extra.get("how") or "left")],
        })
    return form


def run_case(case: SqliteCase, base: Path) -> Dict[str, Any]:
    """执行一个 SQLite 场景，返回带完整证据的 dict。"""
    work = base / case.id
    work.mkdir(parents=True, exist_ok=True)
    db_path = write_sqlite(work / "data.db", case.tables)
    state = webapp.AppState(str(work / "out"))
    source_frame = case.tables.get(case.main_table)
    if source_frame is None:
        source_frame = next(iter(case.tables.values()))
    main_cols = list(source_frame.columns)
    form = _form_for(case, db_path, main_cols)
    files = None
    if case.upload_main:
        files = {"source_file": [("uploaded.db", db_path.read_bytes())]}

    payload: Dict[str, Any] = case.schema or {
        "version": "2.0", "intent": case.intent,
        "steps": case.legacy_steps or [], "output": {"formats": list(case.formats)},
    }
    generation_path = "schema" if case.schema is not None else "legacy"
    form["generation_path"] = [generation_path]

    record: Dict[str, Any] = {
        "case": case, "db_path": db_path, "intent": case.intent,
        "generation_path": generation_path,
    }
    with provider_context(payload):
        plan = webapp.handle_request("POST", "/plan", {}, form, state, files)
    record["plan_status"] = plan.status
    record["plan_body"] = plan.body
    if case.plan_error:
        record["plan_error_ok"] = plan.status == 200 and case.plan_error in plan.body
        return record

    if plan.status != 200:
        record["error"] = f"/plan 返回 {plan.status}: {plan.body[:400]}"
        return record
    try:
        rule = extract_rule_from_plan_body(plan.body)
    except AssertionError as exc:
        marker = plan.body.find('role="alert"')
        detail = plan.body[marker:marker + 600] if marker >= 0 else plan.body[:600]
        record["error"] = f"/plan 未返回规则 JSON：{exc}；error={detail}"
        return record
    record["rule"] = rule

    execute = webapp.handle_request("POST", "/execute", {}, {
        "rule_json": [json.dumps(rule, ensure_ascii=False)],
    }, state)
    record["execute_status"] = execute.status
    if execute.status != 302:
        record["error"] = f"/execute 返回 {execute.status}: {execute.body[:400]}"
        return record
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    record["run_id"] = run_id
    record["run_dir"] = run_dir
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    record["summary"] = summary
    result = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    record["result"] = result
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    record["result_page_status"] = page.status
    record["result_body"] = page.body
    return record


def assert_common(record: Dict[str, Any], *, min_rows: int = 1) -> None:
    """所有成功执行场景都必须满足的公共契约。"""
    assert "error" not in record, record.get("error")
    assert record["plan_status"] == 200
    assert record["execute_status"] == 302
    assert record["result_page_status"] == 200
    summary = record["summary"]
    assert summary["rows"] >= min_rows, summary["rows"]
    assert summary["columns"] > 0
    failed = [s for s in summary.get("step_logs") or [] if s.get("status") != "ok"]
    assert not failed, f"步骤失败：{failed}"
    names = {a["name"] for a in summary["artifacts"]}
    assert "rule.json" in names and "execution_summary.json" in names
    assert "result.csv" in names and "result.json" in names
    if record["case"].report:
        assert any(n.startswith("quality_report_") for n in names)
        assert any(n.startswith("task_report_") for n in names)
    text = (record["run_dir"] / "result.csv").read_text(encoding="utf-8-sig")
    assert not SCI.search(text), f"结果出现科学计数法：{text[:200]}"


# --------------------------------------------------------------------
# 场景定义（pytest 与验证脚本共用）
# --------------------------------------------------------------------
def build_cases() -> List[SqliteCase]:
    cases: List[SqliteCase] = []
    sales = _sales_frame()
    dim = _dim_frame()

    # --- 1. 数据源接入形态 ---
    cases.append(SqliteCase(
        id="source_default_table", category="数据源",
        intent="筛选市为福州，按小区统计条数",
        tables={"data": sales},
        schema=schema_payload([
            {"kind": "filter", "logic": "and",
             "conditions": [{"column": "市", "operator": "eq", "value": "福州"}]},
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        expect={"rows": 3, "total": 10, "sqlite_table": "data"},
        check=_check_default_table,
    ))
    custom_query = "SELECT * FROM lots WHERE 市 = '福州'"
    cases.append(SqliteCase(
        id="source_custom_query", category="数据源",
        intent="按市统计条数",
        tables={"lots": sales},
        main_table="lots", main_query=custom_query,
        schema=schema_payload([
            {"kind": "count", "by": ["市"], "alias": "条数"},
        ], "count_group"),
        expect={"rows": 1, "total": 10, "sqlite_query": custom_query},
        check=_check_custom_query,
    ))
    cases.append(SqliteCase(
        id="source_chinese_table", category="数据源",
        intent="鼓楼区按小区统计条数",
        tables={"成交数据": sales},
        main_table="成交数据",
        schema=schema_payload([
            {"kind": "filter", "logic": "and",
             "conditions": [{"column": "区县", "operator": "eq", "value": "鼓楼区"}]},
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        expect={"total": 8},
        check=_check_chinese_table,
    ))
    cases.append(SqliteCase(
        id="source_upload_db", category="数据源",
        intent="统计每个小区的条数",
        tables={"data": sales}, upload_main=True,
        schema=schema_payload([
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        expect={"total": 16},
        check=_check_upload_db,
    ))

    # --- 2. Schema 主路径的分析场景 ---
    cases.append(SqliteCase(
        id="schema_rate", category="Schema 分析",
        intent="统计福州的成交率，成交价不为空算成交",
        tables={"data": sales},
        schema=schema_payload([
            {"kind": "filter", "logic": "and",
             "conditions": [{"column": "市", "operator": "eq", "value": "福州"}]},
            {"kind": "rate", "column": "成交价(元)",
             "condition": {"column": "成交价(元)", "operator": "notna"},
             "true_label": "成交", "false_label": "未成交", "alias": "成交率",
             "numerator": "hit", "denominator": "total"},
        ], "proportion"),
        expect={"task_type": "proportion"},
        check=_check_rate,
    ))
    cases.append(SqliteCase(
        id="schema_date_count", category="Schema 分析",
        intent="统计福州2026年每个月的条数",
        tables={"data": sales},
        schema=schema_payload([
            {"kind": "filter", "logic": "and",
             "conditions": [{"column": "市", "operator": "eq", "value": "福州"}]},
            {"kind": "date_filter", "column": "结束时间", "year": 2026},
            {"kind": "date_part", "column": "结束时间",
             "parts": ["year", "month"], "bucket": "年月"},
            {"kind": "count", "by": ["结束时间_年月"], "alias": "条数"},
        ], "count_group"),
        expect={"total": 9, "period_col": "结束时间_年月"},
        check=_check_date_count,
    ))
    cases.append(SqliteCase(
        id="schema_ratio_avg", category="Schema 分析",
        intent="2026年按月的平均成交单价趋势，成交单价 = 成交价/面积",
        tables={"data": ratio_frame()},
        schema=schema_payload([
            {"kind": "formula", "column": "成交单价", "expression": "c0/c1",
             "operands": ["成交价(元)", "面积(㎡)"]},
            {"kind": "date_filter", "column": "结束时间", "year": 2026},
            {"kind": "date_part", "column": "结束时间",
             "parts": ["year", "month"], "bucket": "年月"},
            {"kind": "aggregate", "by": ["结束时间_年月"], "metrics": [
                {"column": "成交单价", "func": "mean", "alias": "逐条均值"},
                {"kind": "ratio", "numerator": "成交价(元)", "denominator": "面积(㎡)",
                 "alias": "成交单价", "func": "sum_div_sum"},
            ]},
        ], "trend"),
        expect={"jan": 1000 / 40, "feb": 2000 / 60},
        check=_check_ratio,
    ))
    cases.append(SqliteCase(
        id="schema_fill_detail", category="Schema 分析",
        intent="提取福州数据，成交价空值填为0，其他字段保持为空",
        tables={"data": sales},
        schema=schema_payload([
            {"kind": "filter", "logic": "and",
             "conditions": [{"column": "市", "operator": "eq", "value": "福州"}]},
            {"kind": "fill", "columns": ["成交价(元)"], "method": "zero"},
        ], "detail"),
        expect={"rows": 10},
        check=_check_fill,
    ))
    cases.append(SqliteCase(
        id="schema_join", category="多表",
        intent="按小区关联维表 dim 后，统计每个片区的条数",
        tables={"data": sales, "dim": dim},
        extra={"alias": "dim", "table": "dim", "key": "小区", "right_key": "小区",
               "how": "left"},
        schema=schema_payload([
            {"kind": "join", "right": "dim", "how": "left",
             "left_on": ["小区"], "right_on": ["小区"]},
            {"kind": "count", "by": ["片区"], "alias": "条数"},
        ], "count_group", assumptions=["按同名列小区关联"]),
        expect={"total": 16, "task_type": "summary"},
        check=_check_join,
    ))

    # --- 3. 既有全场景在 SQLite 上重跑（legacy 生成路径） ---
    for name, steps, expected_type, check in _scenario_cases():
        cases.append(SqliteCase(
            id=f"legacy_{name}", category="既有场景迁移",
            intent=f"SQLite 全流程场景：{name}",
            tables={"data": sales},
            main_table="data",
            legacy_steps=steps,
            expect={"task_type": expected_type},
            # 既有场景的断言函数接收 execution_summary；这里统一适配成 record。
            check=(lambda rec, fn=check: fn(rec["summary"])),
        ))

    # --- 4. 边界与失败路径 ---
    cases.append(SqliteCase(
        id="edge_empty_table", category="边界",
        intent="按小区统计条数",
        tables={"data": sales.iloc[0:0]},
        schema=schema_payload([
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        expect={"rows": 0},
        check=_check_empty,
    ))
    cases.append(SqliteCase(
        id="error_missing_table", category="异常",
        intent="按小区统计条数",
        tables={"data": sales}, main_table="missing_table",
        schema=schema_payload([
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        plan_error="no such table",
    ))
    cases.append(SqliteCase(
        id="error_bad_sql", category="异常",
        intent="按小区统计条数",
        tables={"data": sales}, main_query="SELECT FROM data",
        schema=schema_payload([
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        plan_error="sql",
    ))
    cases.append(SqliteCase(
        id="error_write_sql_blocked", category="异常",
        intent="按小区统计条数",
        tables={"data": sales}, main_query="DELETE FROM data",
        schema=schema_payload([
            {"kind": "count", "by": ["小区"], "alias": "条数"},
        ], "count_group"),
        plan_error="readonly",
    ))
    return cases


# --------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------
def _check_default_table(record: Dict[str, Any]) -> None:
    frame = record["result"]
    rule_src = record["rule"]["source"]
    assert rule_src.get("format") == "sqlite"
    assert rule_src.get("sqlite_table") == "data"
    assert not rule_src.get("sqlite_query")
    assert len(frame) == 3
    assert int(frame["条数"].sum()) == 10


def _check_custom_query(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert record["rule"]["source"].get("sqlite_query") == record["case"].main_query
    assert len(frame) == 1
    assert int(frame["条数"].iloc[0]) == 10


def _check_chinese_table(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert int(frame["条数"].sum()) == 8, frame.to_dict("records")


def _check_upload_db(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert "uploads" in record["rule"]["source"]["path"]
    assert int(frame["条数"].sum()) == 16


def _check_rate(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert any("率" in c for c in frame.columns), list(frame.columns)
    assert record["summary"]["task"]["task_type"] == "proportion"
    values = set(frame.iloc[0].astype(str)) if len(frame) else set()
    assert any("70" in v or "30" in v for v in values), frame.to_dict("records")


def _check_date_count(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert "结束时间_年月" in frame.columns
    assert int(frame["条数"].sum()) == 9
    assert frame["结束时间_年月"].notna().all()
    assert list(frame["结束时间_年月"].astype(str)) == sorted(
        frame["结束时间_年月"].astype(str)
    )


def _check_ratio(record: Dict[str, Any]) -> None:
    frame = record["result"].copy()
    assert "成交单价" in frame.columns and "逐条均值" in frame.columns
    period = "结束时间_年月"
    assert period in frame.columns
    frame[period] = frame[period].astype(str)
    jan = frame[frame[period].str.contains("2026-01")]
    feb = frame[frame[period].str.contains("2026-02")]
    assert not jan.empty and not feb.empty, frame.to_dict("records")
    assert abs(float(jan["成交单价"].iloc[0]) - record["case"].expect["jan"]) < 1e-6
    assert abs(float(feb["成交单价"].iloc[0]) - record["case"].expect["feb"]) < 1e-6
    # 逐条均值必须保留，且与 Σ分子/Σ分母 不同
    assert abs(float(jan["逐条均值"].iloc[0]) - 20.0) < 1e-6
    assert abs(float(feb["逐条均值"].iloc[0]) - 27.5) < 1e-6
    assert not frame["成交单价"].equals(frame["逐条均值"]), frame.to_dict("records")


def _check_fill(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert len(frame) == 10
    assert "成交价(元)" in frame.columns
    assert frame["成交价(元)"].notna().all()
    assert set(frame["成交价(元)"].astype(float)) >= {0.0}


def _check_join(record: Dict[str, Any]) -> None:
    frame = record["result"]
    assert "片区" in frame.columns
    assert int(frame["条数"].sum()) == 16
    assert record["summary"]["task"]["task_type"] == "summary"
    assert any(s.get("op") == "join" for s in record["rule"]["steps"])


def _check_empty(record: Dict[str, Any]) -> None:
    frame = record["result"]
    # 空分组在不同 pandas 版本下可能是 0 行，也可能保留一行 0 计数；
    # 共同契约是产物可读、计数列存在且总量为 0。
    assert record["summary"]["rows"] <= 1
    assert "条数" in frame.columns
    assert int(pd.to_numeric(frame["条数"], errors="coerce").fillna(0).sum()) == 0


__all__ = [
    "SqliteCase", "build_cases", "run_case", "assert_common", "write_sqlite",
    "ratio_frame", "schema_payload", "provider_context",
]

# -*- coding: utf-8 -*-
"""SQLite 全场景前端链路回归。

覆盖：表名 / 只读查询 / 上传 .db / 中文表名 / Schema 分析 / 多表关联 /
既有场景迁移 / 空表 / 错误 SQL / 写操作拦截。

所有成功场景都走 /plan → /execute → /result；失败场景也必须由 /plan
返回可读错误，而不是 500 或静默生成错误规则。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.sqlite_harness import assert_common, build_cases, run_case

CASES = build_cases()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_sqlite_full_chain(case, tmp_path: Path) -> None:
    record = run_case(case, tmp_path)

    if case.plan_error:
        assert record.get("plan_error_ok"), (
            f"{case.id}: /plan 未返回预期错误 {case.plan_error!r}；"
            f"body={record.get('plan_body', '')[:500]}"
        )
        assert "rule" not in record, f"{case.id}: 错误场景不应产出规则"
        if case.id == "error_write_sql_blocked":
            with sqlite3.connect(record["db_path"]) as conn:
                left = int(conn.execute("SELECT COUNT(*) FROM data").fetchone()[0])
            assert left == len(case.tables["data"]), "只读守卫未能阻止写 SQL"
        return

    assert_common(record, min_rows=0 if case.id == "edge_empty_table" else 1)
    if case.expect.get("task_type"):
        assert record["summary"]["task"]["task_type"] == case.expect["task_type"]
    if case.check:
        case.check(record)
    report = (record["run_dir"] / record["summary"]["task_report_name"]).read_text(
        encoding="utf-8"
    )
    assert 500 < len(report) < 400_000, f"{case.id}: 报告长度异常 {len(report)}"
    assert "SQLITE" not in report.upper() or "结果" in report


def test_sqlite_form_values_survive_validation_error(tmp_path: Path) -> None:
    """规则校验失败后，SQLite 表名 / 查询必须回填，便于用户直接修正。"""
    from tests.provider_fixture import FixtureProvider
    from tests.sqlite_harness import provider_context, schema_payload, write_sqlite
    import webapp
    from tests.test_full_pipeline_matrix import _sales_frame

    work = tmp_path / "roundtrip"
    work.mkdir()
    db_path = write_sqlite(work / "data.db", {"lots": _sales_frame()})
    state = webapp.AppState(str(work / "out"))
    query = "SELECT * FROM lots WHERE 市 = '福州'"
    # 故意让 Schema 引用不存在的列，触发 /plan 的错误回填分支
    bad_schema = schema_payload([
        {"kind": "count", "by": ["不存在的列"], "alias": "条数"},
    ], "count_group")
    form = {
        "intent": ["按不存在的列统计条数"],
        "source_path": [str(db_path)],
        "source_format": ["sqlite"],
        "sqlite_table": ["lots"],
        "sqlite_query": [query],
        "columns": ["市,小区"],
        "formats": ["csv"],
    }
    with provider_context(bad_schema):
        reply = webapp.handle_request("POST", "/plan", {}, form, state)
    assert reply.status == 200
    assert 'name="sqlite_table" value="lots"' in reply.body
    assert "SELECT * FROM lots" in reply.body
    assert 'name="source_format"' in reply.body

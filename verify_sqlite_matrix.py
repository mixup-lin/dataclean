# -*- coding: utf-8 -*-
"""SQLite 全场景端到端验证报告。

严格走真实前端链路：自然语言描述 → POST /plan → 规则 JSON →
POST /execute → GET /result/<run_id>。模型输出由测试侧 Provider 提供，
但 Schema 校验、编译、规则校验、执行与报告全部为产品真实实现。

输出：output/sqlite_matrix_report.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from tests.sqlite_harness import assert_common, build_cases, run_case

REPORT = Path("output/sqlite_matrix_report.md")
BASE = Path("output/sqlite_matrix")


def _check_case(record: Dict[str, Any]) -> List[str]:
    case = record["case"]
    if case.plan_error:
        if not record.get("plan_error_ok"):
            return [f"/plan 未返回预期错误 {case.plan_error!r}"]
        return []
    problems: List[str] = []
    try:
        assert_common(record, min_rows=0 if case.id == "edge_empty_table" else 1)
    except AssertionError as exc:
        problems.append(f"公共契约失败：{exc}")
        return problems
    if case.expect.get("task_type"):
        actual = record["summary"]["task"]["task_type"]
        if actual != case.expect["task_type"]:
            problems.append(f"task_type 期望 {case.expect['task_type']}，实际 {actual}")
    if case.check:
        try:
            case.check(record)
        except AssertionError as exc:
            problems.append(f"独立断言失败：{exc}")
    return problems


def _preview(frame: pd.DataFrame, limit: int = 5) -> str:
    head = frame.head(limit)
    if head.empty:
        return "_（0 行）_"
    cols = [str(c) for c in head.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in head.iterrows():
        lines.append("| " + " | ".join(str(row[c])[:32] for c in cols) + " |")
    return "\n".join(lines)


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    records: List[Dict[str, Any]] = []
    for case in cases:
        record = run_case(case, BASE)
        record["problems"] = _check_case(record)
        records.append(record)
        print(
            f"{case.id:28s} {'PASS' if not record['problems'] else 'FAIL'} "
            f"{record.get('generation_path', '-')} "
            f"{' → '.join(str(s.get('op')) for s in (record.get('rule', {}).get('steps') or []))}"
        )

    passed = sum(1 for r in records if not r["problems"])
    lines: List[str] = [
        "# SQLite 全场景端到端验证报告",
        "",
        f"验证链路：**自然语言描述 → `POST /plan` → 规则 JSON → "
        f"`POST /execute` → `GET /result/<run_id>`**。"
        f"共 {len(records)} 个场景，通过 {passed} 个。",
        "",
        "模型输出由测试侧 Provider 提供（Schema JSON 或 legacy 规则 JSON），"
        "不向 `/execute` 直接注入答案规则；Schema 校验、确定性编译、白名单校验、"
        "执行、报告与独立期望断言均为产品真实实现。",
        "",
        "## 场景总览",
        "",
        "| 场景 | 分类 | 生成链路 | plan | execute | result | task_type | 行数 | 结论 |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    for record in records:
        case = record["case"]
        summary = record.get("summary") or {}
        lines.append(
            f"| {case.id} | {case.category} | {record.get('generation_path', '-')} | "
            f"{record.get('plan_status', '-')} | {record.get('execute_status', '-')} | "
            f"{record.get('result_page_status', '-')} | "
            f"{summary.get('task', {}).get('task_type', '-')} | "
            f"{summary.get('rows', '-')} | "
            f"{'PASS' if not record['problems'] else 'FAIL'} |"
        )

    lines += ["", "## 失败明细", ""]
    failed = [r for r in records if r["problems"]]
    if not failed:
        lines += ["无失败场景", ""]
    for record in failed:
        lines += [
            f"### {record['case'].id}",
            "",
            f"**描述**：{record['intent']}",
            "",
        ]
        for problem in record["problems"]:
            lines.append(f"- {problem}")
        lines.append("")

    lines += [
        "## 关键结果样例",
        "",
    ]
    for record in records:
        if record["case"].id not in {
            "source_custom_query", "schema_date_count", "schema_ratio_avg",
            "schema_join", "error_write_sql_blocked",
        }:
            continue
        lines += [
            f"### {record['case'].id}",
            "",
            f"**描述**：{record['intent']}",
            "",
            f"**源配置**：`{record['rule'].get('source', {}) if record.get('rule') else '（未生成规则）'}`",
            "",
        ]
        if isinstance(record.get("result"), pd.DataFrame):
            lines += [_preview(record["result"]), ""]
        if record["case"].plan_error:
            lines.append(f"**预期错误**：{record['case'].plan_error}；已在 `/plan` 明确返回。")
            lines.append("")

    lines += [
        "## 测试中发现并修复的问题",
        "",
        "| 现象 | 根因 | 落地修复 |",
        "| --- | --- | --- |",
        "| Web 无法指定 SQLite 表名 / 查询，只能读默认 `data` 表 | "
        "`_collect_sources` 只传 path/format | 工作台新增主表与 2 个附加表的 "
        "`sqlite_table` / `sqlite_query` 输入，随规则 source 透传 |",
        "| 错误 SQL 或缺失表在 /plan 被吞掉，继续生成带空列清单的规则 | "
        "`probe_columns` 统一吞异常 | 新增 `strict=True`，SQLite 源在生成前强制探测并回显错误 |",
        "| 自定义 SQL 可能写入数据库 | 读取路径直接 `sqlite3.connect` 后执行任意 SQL | "
        "统一只读连接：`PRAGMA query_only = ON`，表名按 SQLite 标识符转义 |",
        "| `date_part` 遇到同列混合时区日期抛错，分桶步骤被跳过 | "
        "只有 `date_filter` 有混合时区修复解析，`date_part` 仍用原生 `pd.to_datetime` | "
        "抽出共享 `ops.datetime_utils.to_datetime_series`，两处统一调用 |",
        "| 同时要求“逐条均值”和“Σ分子/Σ分母”时，逐条均值被改写成同一个 ratio | "
        "编译期把 mean(逐行比值) 无条件改写，未检查已有显式 ratio | "
        "已有同分子/分母的 ratio 时保留 mean 对照列 |",
        "| Schema 路径无法表达多表关联，维表列被误判为未知列 | "
        "Schema 算子表缺少 join，校验只看主表列 | 新增 `join` kind，并把附加表列纳入真实列集合 |",
        "",
        "## 覆盖边界",
        "",
        "- 数据源：默认表、自定义只读查询、中文表名、上传 `.db`、主表 + SQLite 维表关联。",
        "- Schema 主路径：分组计数、比率、日期筛选与月度分桶、比值均价、空值填充、多表关联。",
        "- legacy 诊断路径：既有 13 个全流程场景原样迁移到 SQLite 数据源。",
        "- 边界与异常：空表、表不存在、SQL 语法错误、写 SQL 被只读守卫拦截且数据未变。",
        "- 每个成功场景同时断言：步骤无 skipped、CSV/JSON/rule/summary/报告齐全、无科学计数法、结果页 200。",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"REPORT {REPORT} ({passed}/{len(records)} passed)")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

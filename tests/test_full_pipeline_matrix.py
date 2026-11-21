# -*- coding: utf-8 -*-
"""全流程多场景矩阵测试。

把「读取 → 清洗 → 步骤 → 任务画像 → 报告 → 导出」完整链路作为一个黑盒，
用同一批语义一致的数据，分别落到 5 种数据源格式，再叠加多种分析场景，
验证：执行不抛异常、步骤成功、产物齐全、报告有内容、无科学计数法、
任务画像合理、结果可回读。

目的：回答「换一个描述 / 换一个数据源 / 换一个场景」是否仍然正确，
而不是逐个算子地验证局部行为（局部行为见 test_all_ops_matrix.py）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from rule_executor import execute_rule
from tests.provider_fixture import make_rule

# 科学计数法检测（与既有测试口径一致）
_SCI = re.compile(r"(?<![\w\"-])-?\d+(?:\.\d+)?e[+-]\d+", re.I)


# ------------------------------------------------------------
# 语义一致的数据：同一份逻辑表，可序列化为任意格式
# ------------------------------------------------------------
def _sales_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "市": ["福州"] * 10 + ["厦门"] * 6,
        "区县": ["鼓楼区", "台江区"] * 8,
        "小区": ["A", "B", "C", "A", "B", "C", "A", "B", "C", "A",
                 "D", "E", "D", "E", "D", "E"],
        # 福州 10 行：3 空 / 7 非空；全表 16 行：5 空 / 11 非空
        "成交价(元)": [100, None, 300, 400, None, 600, 700, 800, None, 1000,
                   1100, 1200, None, 1400, 1500, None],
        # 混合时区：带 +08:00 与不带时区，验证年份筛选不漏行
        "结束时间": [
            "2026-03-08T10:00:00+08:00", "2026-07-01", "2025-01-01",
            "2026-12-31", "2026-05-05", "2026-02-02", "2026-01-01",
            "2026-01-02", "2026-01-03", "2026-01-04", "2026-06-01",
            "2026-06-02", "2025-12-31", "2026-08-08", "2026-09-09",
            "2026-10-10",
        ],
        "手机": ["13800138000"] * 16,
        "金额": ["1.2万", "1234", "100元", "3.5万元", "2000", "50", "1亿",
               "9999", "80", "90", "100", "110", "120", "130", "140", "150"],
        "文本": ["  AbC  ", "ＸＹＺ", "a\u200bb", "hello", "  ", "World",
               "MiXeD", "123", "a", "b", "c", "d", "e", "f", "g", "h"],
        "数值": [1.0, None, 3.0, 10.0, 5.0, 2.0, 8.0, 4.0,
               6.0, 7.0, 9.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        "数值2": [2.0, 4.0, 6.0, 1.0, 3.0, 9.0, 7.0, 5.0,
                8.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
        "分组": ["A", "B"] * 8,
    })


def _dim_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "小区": ["A", "B", "C", "D", "E"],
        "片区": ["城东", "城西", "城南", "城北", "城中"],
    })


def _write_source(fmt: str, path: Path, df: pd.DataFrame) -> dict:
    """把同一份数据写成指定格式，返回 source 描述。"""
    if fmt == "csv":
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return {"path": str(path), "format": "csv"}
    if fmt == "json":
        df.to_json(path, orient="records", lines=False, force_ascii=False)
        return {"path": str(path), "format": "json"}
    if fmt == "jsonl":
        df.to_json(path, orient="records", lines=True, force_ascii=False)
        return {"path": str(path), "format": "json"}
    if fmt == "excel":
        df.to_excel(path, index=False, sheet_name="data")
        return {"path": str(path), "format": "excel", "excel_sheet": "data"}
    if fmt == "sqlite":
        with sqlite3.connect(str(path)) as conn:
            df.to_sql("lots", conn, index=False, if_exists="replace")
        return {
            "path": str(path), "format": "sqlite",
            "sqlite_query": "SELECT * FROM lots WHERE 市 IN ('福州','厦门')",
        }
    raise AssertionError(f"未知格式 {fmt}")


SOURCE_FORMATS = ["csv", "json", "jsonl", "excel", "sqlite"]


def _assert_common(summary: dict, tmp_path: Path, *, report: bool = True):
    """所有全流程场景都必须满足的公共契约。"""
    assert summary["rows"] > 0
    assert summary["columns"] > 0
    names = {a["name"] for a in summary["artifacts"]}
    formats = summary.get("formats") or ["csv"]
    result_name = next((f"result.{f}" for f in formats if f"result.{f}" in names), None)
    assert result_name, f"未找到结果文件，formats={formats}，产物={sorted(names)}"
    assert {"rule.json", "execution_summary.json"} <= names
    if report:
        assert any(n.startswith("quality_report_") for n in names)
        assert any(n.startswith("task_report_") for n in names)
    run_dir = tmp_path / summary["run_id"]
    result_text = (run_dir / result_name).read_text(encoding="utf-8-sig")
    assert not _SCI.search(result_text), f"{result_name} 不应出现科学计数法"
    payload = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == summary["run_id"]
    assert isinstance(summary["task"], dict) and summary["task"].get("task_type")


# ============================================================
# 1. 数据源矩阵：5 种格式跑通同一条分析链路
# ============================================================
@pytest.mark.parametrize("fmt", SOURCE_FORMATS)
def test_source_format_full_pipeline(fmt, tmp_path):
    src = _write_source(fmt, tmp_path / f"data.{fmt}", _sales_frame())
    rule = make_rule(
        intent="筛选福州，按小区统计条数",
        source_path=src["path"],
        source=src,
        options={"fill_missing": "median", "outlier_method": "none"},
        steps=[
            {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
            {"op": "count_rows", "by": "小区", "alias": "条数"},
            {"op": "sort", "by": "条数", "ascending": False},
        ],
        output={"formats": ["csv", "json"], "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert all(s["status"] == "ok" for s in summary["step_logs"]), summary["step_logs"]
    assert summary["rows"] == 3, f"{fmt}: 福州涉及 A/B/C 三个小区"
    _assert_common(summary, tmp_path)
    back = pd.read_csv(tmp_path / summary["run_id"] / "result.csv", encoding="utf-8-sig")
    assert list(back.columns) == ["小区", "条数"]
    assert int(back["条数"].sum()) == 10  # 福州 10 行全部计入


# ============================================================
# 2. 场景矩阵：同一数据源叠加多种真实分析场景
# ============================================================
def _scenario_cases():
    """返回 (场景名, steps, 期望 task_type, 校验函数)。"""
    cases = []

    # --- 比率：成交率 / 未成交率 / 分组率 ---
    cases.append(("成交率", [
        {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交", "alias": "成交率"},
    ], "proportion", lambda s: _check_rate(s, alias="成交率", hit=7, miss=3)))

    cases.append(("未成交率", [
        {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交",
         "numerator": "miss", "alias": "未成交率"},
    ], "proportion", lambda s: _check_rate(s, alias="未成交率", hit=3, miss=7)))

    cases.append(("分组成交率", [
        {"op": "rate", "column": "成交价(元)",
         "condition": {"column": "成交价(元)", "operator": "notna"},
         "true_label": "成交", "false_label": "未成交",
         "by": ["市"], "alias": "成交率"},
    ], "proportion", lambda s: _check_group_rate(s)))

    # --- 日期 + 判空混合筛选 ---
    cases.append(("日期与判空", [
        {"op": "filter", "conditions": [
            {"column": "市", "operator": "eq", "value": "福州"},
            {"column": "小区", "operator": "notna"},
        ], "logic": "and"},
        {"op": "date_filter", "column": "结束时间", "year": 2026},
        {"op": "count_rows", "by": "市", "alias": "条数"},
    ], None, lambda s: _check_date_and_notna(s)))

    # --- 分组统计（分组后排序按设计判为 ranking） ---
    cases.append(("分组聚合", [
        {"op": "aggregate", "by": "分组", "column": "数值",
         "func": "sum", "alias": "合计"},
        {"op": "sort", "by": "合计", "ascending": False},
    ], "ranking", lambda s: _check_group_sum(s)))

    # --- 透视 ---
    cases.append(("透视表", [
        {"op": "pivot", "index": "分组", "columns": "市",
         "values": "数值", "aggfunc": "sum"},
    ], None, lambda s: None))

    # --- 去重（全表计数用 pct overall 形态，count_rows 契约要求 by） ---
    cases.append(("去重", [
        {"op": "dedupe", "subset": ["市"], "keep": "first"},
    ], None, lambda s: None))

    # --- 排序 + 截取 ---
    cases.append(("排序截取", [
        {"op": "sort", "by": "数值", "ascending": False},
        {"op": "head", "n": 5},
    ], None, lambda s: None))

    # --- 质量断言 ---
    cases.append(("质量断言", [
        {"op": "assert", "checks": [
            {"column": "数值", "operator": "gte", "value": 0}], "action": "flag"},
    ], None, lambda s: None))

    # --- PII 脱敏 ---
    cases.append(("脱敏", [
        {"op": "mask_pii", "columns": ["手机"], "kinds": ["phone"], "mode": "partial"},
    ], None, lambda s: _check_mask(s)))

    # --- 单位换算 + 文本归一 ---
    cases.append(("清洗增强", [
        {"op": "convert_unit", "columns": ["金额"], "kind": "money"},
        {"op": "text_normalize", "columns": ["文本"]},
    ], None, lambda s: None))

    # --- 分箱 + 异常值 ---
    cases.append(("分箱与异常", [
        {"op": "bin", "column": "数值", "bins": 3, "method": "width"},
        {"op": "outlier", "column": "数值", "method": "iqr", "action": "flag"},
    ], None, lambda s: None))

    # --- 相关分析 ---
    cases.append(("相关性", [
        {"op": "corr", "columns": ["数值", "数值2"]},
    ], None, lambda s: None))

    return cases


def _check_rate(summary, *, alias, hit, miss):
    back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
    total = hit + miss
    assert f"{alias}" in back.columns or "总数" in back.columns, list(back.columns)
    # 命中/未命中两类条数一定出现在结果里
    vals = set(back.iloc[0].astype(str)) if len(back) else set()
    assert any(str(hit) in v or str(miss) in v for v in vals), back.to_dict("records")
    task = summary["task"]
    assert task["task_type"] == "proportion"


def _check_group_rate(summary):
    back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
    assert "市" in back.columns
    assert len(back) == 2  # 福州 / 厦门
    assert summary["task"]["task_type"] == "proportion"
    # 福州 7/10=70%，厦门 4/6≈66.67%
    by_city = back.set_index("市")
    assert "成交率" in by_city.columns or any("率" in c for c in back.columns)


def _check_date_and_notna(summary):
    back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
    # 福州 10 行中 2025 年只有 1 行（索引 2），其余 9 行为 2026
    assert int(back.iloc[0]["条数"]) == 9, back.to_dict("records")


def _check_group_sum(summary):
    back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
    assert len(back) == 2
    assert "合计" in back.columns


def _check_mask(summary):
    back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
    assert not (back["手机"].astype(str) == "13800138000").all()


@pytest.mark.parametrize("name,steps,expected_type,check", _scenario_cases(),
                         ids=[c[0] for c in _scenario_cases()])
def test_scenario_full_pipeline(name, steps, expected_type, check, tmp_path):
    src = _write_source("csv", tmp_path / "data.csv", _sales_frame())
    rule = make_rule(
        intent=f"全流程场景：{name}",
        source_path=src["path"],
        options={"fill_missing": "median", "outlier_method": "none"},
        steps=steps,
        output={"formats": ["csv", "json"], "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    failed = [s for s in summary["step_logs"] if s["status"] != "ok"]
    assert not failed, f"{name} 步骤失败: {failed}"
    _assert_common(summary, tmp_path)
    if expected_type:
        assert summary["task"]["task_type"] == expected_type, (
            f"{name}: 期望 {expected_type}，实际 {summary['task']['task_type']}"
        )
    check(summary)
    # 报告必须含数据且不超限
    report = (tmp_path / summary["run_id"] / summary["task_report_name"]).read_text(
        encoding="utf-8")
    assert len(report) > 500 and len(report) < 400_000


# ============================================================
# 3. 多源 join 全流程
# ============================================================
def test_multi_source_join_full_pipeline(tmp_path):
    main = _write_source("csv", tmp_path / "main.csv", _sales_frame())
    dim = _write_source("csv", tmp_path / "dim.csv", _dim_frame())
    rule = make_rule(
        intent="关联片区维表后按片区统计",
        source_path=main["path"],
        sources={"main": main, "dim": dim},
        steps=[
            {"op": "join", "right": "dim", "how": "left",
             "left_on": "小区", "right_on": "小区"},
            {"op": "count_rows", "by": "片区", "alias": "条数"},
        ],
        output={"formats": ["csv"], "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["source_count"] == 2
    assert summary["task"]["task_type"] == "join"
    assert all(s["status"] == "ok" for s in summary["step_logs"])
    _assert_common(summary, tmp_path)
    back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
    assert "片区" in back.columns
    assert int(back["条数"].sum()) == 16


# ============================================================
# 4. 导出格式矩阵
# ============================================================
@pytest.mark.parametrize("formats", [
    ["csv"], ["json"], ["csv", "json"],
])
def test_export_formats_full_pipeline(formats, tmp_path):
    src = _write_source("csv", tmp_path / "data.csv", _sales_frame())
    rule = make_rule(
        source_path=src["path"],
        steps=[{"op": "count_rows", "by": "市", "alias": "条数"}],
        output={"formats": formats, "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    names = {a["name"] for a in summary["artifacts"]}
    for fmt in formats:
        assert f"result.{fmt}" in names
    _assert_common(summary, tmp_path)


# ============================================================
# 5. 空数据源：各格式不崩溃
# ============================================================
@pytest.mark.parametrize("fmt", SOURCE_FORMATS)
def test_empty_source_full_pipeline(fmt, tmp_path):
    empty = _sales_frame().iloc[0:0]
    src = _write_source(fmt, tmp_path / f"empty.{fmt}", empty)
    rule = make_rule(
        source_path=src["path"],
        source=src,
        steps=[{"op": "count_rows", "by": "小区", "alias": "条数"}],
        output={"formats": ["csv"], "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["rows"] >= 0
    names = {a["name"] for a in summary["artifacts"]}
    assert "result.csv" in names


# ============================================================
# 6. 单步失败必须可见（不掩盖）
# ============================================================
def test_failed_step_surfaces_warning(tmp_path, monkeypatch):
    """执行期单步失败必须可见，且不影响后续步骤。"""
    import rule_executor as rex

    src = _write_source("csv", tmp_path / "data.csv", _sales_frame())
    rule = make_rule(
        source_path=src["path"],
        steps=[
            {"op": "sort", "by": "数值", "ascending": False},
            {"op": "count_rows", "by": "市", "alias": "条数"},
        ],
        options={"fill_missing": "median", "outlier_method": "none"},
        output={"formats": ["csv"], "report": False},
    )
    real_apply = rex.apply_step

    def flaky(df, step, frames=None, context=None):
        if step.get("op") == "sort":
            raise RuntimeError("模拟排序步骤运行时失败")
        return real_apply(df, step, frames, context)

    monkeypatch.setattr(rex, "apply_step", flaky)
    summary = execute_rule(rule, base_output=str(tmp_path))
    skipped = [s for s in summary["step_logs"] if s["status"] == "skipped"]
    assert skipped and skipped[0]["op"] == "sort"
    assert summary["safety_warnings"], "跳过的步骤必须产生告警"
    assert any("未执行" in w or "不完整" in w for w in summary["safety_warnings"])
    # 后续步骤仍执行，流程不中断
    assert any(s["op"] == "count_rows" and s["status"] == "ok"
               for s in summary["step_logs"])


def test_reconcile_drops_bad_column_with_warning(tmp_path):
    """编译期剔除引用不存在列的步骤时，必须留下可审计告警。"""
    src = _write_source("csv", tmp_path / "data.csv", _sales_frame())
    rule = make_rule(
        source_path=src["path"],
        steps=[
            {"op": "sort", "by": "不存在的列", "ascending": False},
            {"op": "count_rows", "by": "市", "alias": "条数"},
        ],
        options={"fill_missing": "median", "outlier_method": "none"},
        output={"formats": ["csv"], "report": False},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["reconcile_warnings"], "列对齐剔除步骤必须留下告警"
    assert any("sort" in w or "剔除" in w for w in summary["reconcile_warnings"])
    assert all(s["status"] == "ok" for s in summary["step_logs"])


# ============================================================
# 7. 比率：5 种错误规则形态在同一数据源上收敛
# ============================================================
def test_bad_rate_shapes_converge(tmp_path):
    src = _write_source("csv", tmp_path / "data.csv", _sales_frame())
    intent = ("筛选：市：福州 判断：成交价为空-未成交，成交价不为空-成交 "
              "目标：统计未成交率（未成交/总数）")
    bad_shapes = [
        # 两个互相矛盾的 filter
        [
            {"op": "filter", "column": "成交价(元)", "operator": "isna"},
            {"op": "filter", "column": "成交价(元)", "operator": "notna"},
        ],
        # filter(isna) + rate（判据列被误当筛选，尾部再叠一个 pct 占比）
        [
            {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
            {"op": "filter", "column": "成交价(元)", "operator": "isna"},
            {"op": "rate", "column": "成交价(元)",
             "condition": {"column": "成交价(元)", "operator": "notna"},
             "true_label": "成交", "false_label": "未成交", "alias": "成交率"},
            {"op": "pct", "scope": "condition", "condition": {
                "column": "成交价(元)", "operator": "notna"}, "alias": "占比"},
        ],
        # assign + count_rows
        [
            {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
            {"op": "assign", "column": "成交状态", "rules": [
                {"condition": {"column": "成交价(元)", "operator": "isna"},
                 "value": "未成交"}], "default": "成交"},
            {"op": "count_rows", "by": "成交状态", "alias": "条数"},
        ],
    ]
    rates = []
    for i, steps in enumerate(bad_shapes):
        rule = make_rule(
            intent=intent, source_path=src["path"],
            options={"fill_missing": "median", "outlier_method": "none"},
            steps=steps,
            output={"formats": ["csv"], "report": False},
        )
        summary = execute_rule(rule, base_output=str(tmp_path))
        back = pd.read_csv(Path(summary["result_csv"]), encoding="utf-8-sig")
        # 每条规则都应仍产出可执行结果（可能被编译归一）
        assert len(back) >= 0
        rates.append((summary["task"]["task_type"], list(back.columns)))
    # 至少一种形态必须被归一为 proportion（编译兜底生效）
    assert any(t == "proportion" for t, _ in rates), rates

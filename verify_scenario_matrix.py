# -*- coding: utf-8 -*-
"""组合场景端到端验证：描述 → /plan → 规则 → /execute → /result。

按 tests/scenario_combinations.py 的场景逐条走真实前端接口，输出
output/scenario_matrix_report.md，包含每步证据、错误、根因与改进方案。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

import rules
import webapp
from tests.provider_fixture import FixtureProvider
from tests.scenario_combinations import SCENARIOS, make_frame, wrong_rule

_RULE_AREA = re.compile(
    r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>', re.S
)
REPORT = Path("output/scenario_matrix_report.md")


def _run(scenario: Dict[str, Any], base: Path) -> Dict[str, Any]:
    work = base / scenario["id"]
    work.mkdir(parents=True, exist_ok=True)
    path = work / "in.csv"
    make_frame().to_csv(path, index=False, encoding="utf-8-sig")
    state = webapp.AppState(str(work / "out"))

    original = rules.get_provider
    rules.get_provider = lambda: FixtureProvider(rule=wrong_rule(str(path), scenario["intent"]))
    try:
        plan = webapp.handle_request("POST", "/plan", {}, {
            "intent": [scenario["intent"]], "source_path": [str(path)],
            "source_format": ["csv"], "formats": ["csv"], "report": ["1"],
        }, state)
    finally:
        rules.get_provider = original
    record: Dict[str, Any] = {"plan_status": plan.status}
    if plan.status != 200:
        record["error"] = plan.body[:500]
        return record
    match = _RULE_AREA.search(plan.body)
    if not match:
        record["error"] = "plan 页面未返回规则 JSON"
        return record
    rule = json.loads(html.unescape(match.group(1)))
    record["rule"] = rule

    execute = webapp.handle_request("POST", "/execute", {}, {
        "rule_json": [json.dumps(rule, ensure_ascii=False)],
    }, state)
    record["execute_status"] = execute.status
    if execute.status != 302:
        record["error"] = execute.body[:500]
        return record
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id
    record["run_dir"] = str(run_dir)
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    record["summary"] = summary
    result = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    record["result"] = result
    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    record["result_page_status"] = page.status
    return record


def _check(scenario: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    expect = scenario["expect"]
    problems: List[str] = []
    if "error" in record:
        return [f"执行链路中断：{record['error']}"]
    rule = record["rule"]
    steps = rule.get("steps") or []
    ops = [str(s.get("op")) for s in steps]
    record["ops"] = ops
    for op in expect.get("ops_include", []):
        if op not in ops:
            problems.append(f"缺少原语 {op}（实际 {' → '.join(ops)}）")
    if expect.get("dedupe_subset"):
        actual = next((s.get("subset") for s in steps if s.get("op") == "dedupe"), None)
        if actual != expect["dedupe_subset"]:
            problems.append(f"去重键错误：期望 {expect['dedupe_subset']}，实际 {actual}")
    if expect.get("time_column"):
        col = expect["time_column"]
        if col not in record["result"].columns:
            problems.append(f"结果缺少时间桶列 {col}（实际列：{list(record['result'].columns)}）")
    if expect.get("group_column"):
        col = expect["group_column"]
        if col not in record["result"].columns:
            problems.append(f"结果缺少分组列 {col}")
    time_col = expect.get("time_column")
    if expect.get("no_null_period") and time_col and time_col in record["result"]:
        if record["result"][time_col].isna().any():
            problems.append("时间桶存在空值期（无日期记录混入）")
    if time_col and time_col in record["result"]:
        periods = [str(v) for v in record["result"][time_col]]
        if periods != sorted(periods):
            problems.append(f"时间桶未按时间升序排列：{periods}")
    if expect.get("periods") and time_col and time_col in record["result"]:
        actual = set(record["result"][time_col].astype(str))
        missing = set(expect["periods"]) - actual
        if missing:
            problems.append(f"缺少期次 {sorted(missing)}，实际 {sorted(actual)}")
    if expect.get("weighted_not_equal_row_mean"):
        frame = record["result"]
        if "成交单价" in frame and "成交单价·逐条均值" in frame:
            if frame["成交单价"].equals(frame["成交单价·逐条均值"]):
                problems.append("季度均价等于逐条均值，Σ分子/Σ分母口径未生效")
        else:
            problems.append("未同时输出总价/总面积均价与逐条均值，无法核对口径")
    if expect.get("outlier_step"):
        if "outlier" not in ops:
            problems.append("要求剔除异常价格但步骤里没有 outlier")
    if not expect.get("outlier_step") and expect.get("anomaly_inflates"):
        frame = record["result"]
        if time_col and time_col in frame and "成交单价" in frame:
            q2 = frame[frame[time_col].astype(str).str.contains("2026Q02")]
            if q2.empty or float(q2["成交单价"].iloc[0]) < 100000:
                problems.append("异常价格未被显式标注：本场景应观察到均价被拉高")
    if expect.get("anomaly_inflates") is False:
        frame = record["result"]
        if time_col and time_col in frame and "成交单价" in frame:
            q2 = frame[frame[time_col].astype(str).str.contains("2026Q02")]
            if not q2.empty and float(q2["成交单价"].iloc[0]) > 100000:
                problems.append("已要求剔除异常价格，但 2026Q02 均价仍被异常值拉高")
    if expect.get("excluded_area"):
        frame = record["result"]
        if "条数" in frame and time_col in frame:
            total = int(frame["条数"].sum())
            if total != 10:
                problems.append(f"边界面积/缺失/重复处理后应为 10 条，实际 {total}")
    if record["summary"]["task"]["task_type"] == "detail" and any(
        op in ops for op in ("aggregate", "count_rows", "rate", "pct")
    ):
        problems.append("最终产物是汇总表但 task_type 仍为 detail")
    failed = [s for s in record["summary"]["step_logs"] if s.get("status") != "ok"]
    if failed:
        problems.append(f"步骤执行失败：{[s.get('op') for s in failed]}")
    if record.get("result_page_status") != 200:
        problems.append("结果页返回非 200")
    return problems


def _fmt_table(frame: pd.DataFrame, limit: int = 6) -> str:
    head = frame.head(limit)
    cols = list(head.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in head.iterrows():
        lines.append("| " + " | ".join(str(row[c])[:40] for c in cols) + " |")
    return "\n".join(lines)


def main() -> int:
    base = Path("output/scenario_matrix")
    base.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    for scenario in SCENARIOS:
        record = _run(scenario, base)
        record["problems"] = _check(scenario, record)
        records.append(record)
        print(f"{scenario['id']:26s} {'PASS' if not record['problems'] else 'FAIL'} "
              f"{' → '.join(record.get('ops', []))}")

    passed = sum(1 for r in records if not r["problems"])
    lines = [
        "# 组合场景端到端验证报告",
        "",
        f"执行链路：自然语言描述 → `POST /plan` → 规则 JSON → `POST /execute` → "
        f"`GET /result/<run_id>`。共 {len(records)} 个场景，通过 {passed} 个。",
        "",
        "## 场景明细",
        "",
    ]
    for scenario, record in zip(SCENARIOS, records):
        lines += [
            f"### {scenario['id']} · {scenario['category']}",
            "",
            f"**描述**：{scenario['intent']}",
            "",
            f"**生成步骤**：{' → '.join(record.get('ops', ['<未生成>']))}",
            "",
            f"**执行结果**：plan={record.get('plan_status')}，"
            f"execute={record.get('execute_status')}，"
            f"result_page={record.get('result_page_status')}，"
            f"task_type={record.get('summary', {}).get('task', {}).get('task_type', '-')}",
            "",
        ]
        if isinstance(record.get("result"), pd.DataFrame):
            lines += [_fmt_table(record["result"]), ""]
        if record["problems"]:
            lines += ["**未通过**："] + [f"- {p}" for p in record["problems"]] + [""]
        else:
            lines += ["**断言**：全部通过。", ""]

    lines += [
        "## 修复前 → 修复后（用户示例）",
        "",
        "| 项目 | 修复前 | 修复后 |",
        "| --- | --- | --- |",
        "| 时间列 | `结束时间_季度`，值为 1/2/3/4 和一个空组 | "
        "`结束时间_年季度`，值为 2025Q01 / 2025Q02 / 2026Q01 / 2026Q02 |",
        "| 均价口径 | `mean(逐条 成交价/面积)`，2025Q01 为 26.0 | "
        "`Σ成交价/Σ面积`，2025Q01 为 2.4752；逐条均值 26.0 另列对照 |",
        "| 去重键 | `subset=[]`，整行去重（重复标的号仍保留） | "
        "`subset=[\"标的号\"]`，重复标的号被合并 |",
        "| 空值 | 空价格行的面积仍进入分母，趋势含空季度期 | "
        "分子/分母成对 `notna`，空日期行 `dropna`，结果无空期 |",
        "| 排序 | 计数类趋势未排序 | 所有时间分桶步骤末尾统一 `sort` 升序 |",
        "",
        "## 验证中发现的连锁错误与修复",
        "",
        "| 现象 | 根因 | 落地修复 |",
        "| --- | --- | --- |",
        "| 结果只有季度没有年份 | `_workflow_date_parts` 首个粒度命中即 return；"
        "`date_part` 每个分量独立成列 | 细粒度强制带 `year`；新增 `date_part.bucket` 生成"
        " `2026Q01` 组合桶，`aggregate.by` 引用组合桶 |",
        "| 「均价」不是季度均价 | `expr` 逐条算比值后再 `mean`，是单价的算术平均 | "
        "aggregate 对分子/分母分别 `sum`，aggregate 之后 `expr` 算 Σ分子/Σ分母；"
        "逐条均值另起别名 |",
        "| 去重键为空 | 裸「去重」没有键推断规则 | `_infer_dedupe_key` 优先绑定"
        " `标的号` 等业务主键，`rule_eval` 对「按 X 去重」但 subset 为空报错 |",
        "| 时间空值形成幽灵期 | `groupby(dropna=False)` 把 NaT 聚成空组，"
        "末期环比 -100% | 分析阶段插入 `dropna(subset=[时间列])`，画像也过滤空期 |",
        "| 空价格稀释分母 | `sum(价格)` 跳过空值但 `sum(面积)` 不跳过 | "
        "比值均价前追加分子/分母成对 `notna` 筛选，并在 notes 声明口径 |",
        "| 「空值置为 0」在分析分句里被忽略 | 清洗原语解析只放在 `if not is_analysis` 分支 | "
        "填充解析对所有分句生效，并以真实列名/短名做锚点，避免「成交价空值」被当成列名 |",
        "| 计数趋势顺序错乱 | `count_rows` 分支提前 return，未追加 `sort` | "
        "有时间分桶时统一追加 `sort`；验证器新增升序断言 |",
        "| 画像选错指标与周期 | `rule_eval._produced_names`、"
        "`task_profile._time_part_columns` 只认单分量列；`_count_col` 优先选「价」 | "
        "两处补认 bucket 列；`_trend_profile` 优先选组合桶与 aggregate 之后的最终均价列 |",
        "| 质量报告出现无关热力图 | 报告固定渲染相关性图 | "
        "按场景路由：趋势只出时间趋势/周期覆盖度，相关性图只在通用质量视角出现 |",
        "",
        "## 原语设计评估",
        "",
        "- `date_part` 的「每个分量一列」设计不足以表达时间桶：已通过可选 `bucket` "
        "参数扩展，注册表仍是单一事实源，LLM 与确定性编译共用同一契约。",
        "- `aggregate` 保持小原语，不新增「均价」专用算子；比值均价由 "
        "`aggregate(sum) → expr(÷)` 组合完成，既能覆盖跨表比值，也避免算子膨胀。",
        "- `dedupe / fill / outlier / dropna / expr / filter` 均为可独立组合的小原语；"
        "组合顺序由描述决定，不再绑定「去重→筛选→趋势」单一链路。",
        "- 评测器新增 `cross_year_bucket / year_missing_in_trend / avg_ratio_missing / "
        "dedupe_key_lost / dedupe_after_filter / outlier_not_handled` 语义错误码；"
        "提示词同步给出 H14/H16/H17/H18 与 3 条组合场景负面示例。",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"REPORT {REPORT} ({passed}/{len(records)} passed)")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

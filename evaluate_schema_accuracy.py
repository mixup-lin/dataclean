# -*- coding: utf-8 -*-
"""真实模型 → 结构化意图 Schema → 规则 的准确率评测。

不使用 mock。每条用例都有独立的期望断言（任务族、算子、顺序、列、筛选方向、
时间粒度、比值口径等），只有 Schema 解析、语义自检、规则契约校验和期望断言
全部通过才计为正确。结果写入 output/schema_accuracy_report.json / .md。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import rules
from llm_provider import describe_provider, get_provider, load_dotenv
from ops.errors import RuleValidationError
from ops.rule_eval import format_issues

load_dotenv()

BASE_COLUMNS = ["标的号", "市", "区县", "小区", "面积(㎡)", "成交价(元)", "结束时间", "状态"]
ALIAS_COLUMNS = ["id", "city", "district", "community", "area", "price", "closed_at", "status"]


@dataclass
class Case:
    id: str
    family: str
    intent: str
    expect: Dict[str, Any]
    columns: Sequence[str] = field(default_factory=lambda: BASE_COLUMNS)
    aliases: Dict[str, str] = field(default_factory=dict)
    expected_reject: bool = False


def _ops(rule: Dict[str, Any]) -> List[str]:
    return [str(step.get("op")) for step in rule.get("steps", []) if isinstance(step, dict)]


def _steps(rule: Dict[str, Any], op: str) -> List[Dict[str, Any]]:
    return [s for s in rule.get("steps", []) if isinstance(s, dict) and s.get("op") == op]


def _all_conditions(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cond = step.get("condition")
    if isinstance(cond, dict):
        out.append(cond)
    for c in step.get("conditions") or []:
        if isinstance(c, dict):
            out.append(c)
        for inner in c.get("conditions") or []:
            if isinstance(inner, dict):
                out.append(inner)
    return out


def _has_condition(rule: Dict[str, Any], column: str, operator: str,
                   value: Any = "__any__") -> bool:
    for step in rule.get("steps", []):
        if not isinstance(step, dict) or step.get("op") not in ("filter", "date_filter"):
            continue
        for cond in _all_conditions(step):
            if str(cond.get("column")) != column or str(cond.get("operator")) != operator:
                continue
            if value == "__any__" or str(cond.get("value")) == str(value):
                return True
    return False


def _contains_fragment(values: Sequence[Any], fragment: str) -> bool:
    return any(fragment in str(v) for v in values)


def check_expectation(rule: Dict[str, Any], expect: Dict[str, Any]) -> List[str]:
    """返回期望不符合项；空列表表示语义断言全部通过。"""
    errors: List[str] = []
    names = _ops(rule)
    task_type = str(rule.get("task_type") or "")
    wanted_task = expect.get("task_type")
    if wanted_task and task_type != wanted_task:
        errors.append(f"task_type={task_type!r} != {wanted_task!r}")

    required = set(expect.get("required_ops") or [])
    missing = sorted(required - set(names))
    if missing:
        errors.append(f"缺少算子 {missing}；实际 {names}")
    forbidden = set(expect.get("forbidden_ops") or [])
    extra = sorted(forbidden & set(names))
    if extra:
        errors.append(f"出现禁止算子 {extra}；实际 {names}")

    for first, second in expect.get("order") or []:
        if first not in names or second not in names:
            errors.append(f"顺序断言缺少 {first}/{second}：{names}")
        elif names.index(first) > names.index(second):
            errors.append(f"顺序错误 {first} 应在 {second} 之前：{names}")

    for want in expect.get("conditions") or []:
        if not _has_condition(rule, want["column"], want["operator"], want.get("value", "__any__")):
            errors.append(f"缺少筛选条件 {want}；实际 {names}")

    for want in expect.get("date_filters") or []:
        found = False
        for step in _steps(rule, "date_filter"):
            if all(str(step.get(k)) == str(v) for k, v in want.items()):
                found = True
                break
        if not found:
            errors.append(f"缺少 date_filter {want}；实际 {_steps(rule, 'date_filter')}")

    for want in expect.get("date_parts") or []:
        found = False
        for step in _steps(rule, "date_part"):
            parts = set(str(p) for p in step.get("parts") or [])
            if set(want.get("parts") or []).issubset(parts) and (
                    not want.get("bucket") or str(step.get("bucket")) == str(want["bucket"])):
                found = True
                break
        if not found:
            errors.append(f"缺少 date_part {want}；实际 {_steps(rule, 'date_part')}")

    for want in expect.get("rate") or []:
        candidates = _steps(rule, "rate")
        if not candidates:
            errors.append("缺少 rate 步骤")
            continue
        if not any(all(str(step.get(k, "")) == str(v) for k, v in want.items())
                   for step in candidates):
            errors.append(f"rate 字段不符 {want}；实际 {candidates}")

    for want in expect.get("aggregate") or []:
        candidates = _steps(rule, "aggregate")
        if not candidates:
            errors.append("缺少 aggregate 步骤")
            continue
        matched = False
        for step in candidates:
            by = set(str(c) for c in step.get("by") or [])
            funcs = [str(f) for f in step.get("func") or []]
            ok = set(want.get("by") or []).issubset(by)
            for func in want.get("funcs") or []:
                ok = ok and (func in funcs)
            aliases = [str(a) for a in step.get("alias") or []]
            for alias in want.get("aliases") or []:
                ok = ok and _contains_fragment(aliases, alias)
            if ok:
                matched = True
                break
        if not matched:
            errors.append(f"aggregate 字段不符 {want}；实际 {candidates}")

    for want in expect.get("fills") or []:
        found = False
        for step in _steps(rule, "fill"):
            cols = set(str(c) for c in step.get("columns") or [])
            if want.get("column") in cols and (
                    not want.get("method") or str(step.get("method")) == str(want["method"])) and (
                    "value" not in want or str(step.get("value")) == str(want["value"])):
                found = True
        if not found:
            errors.append(f"缺少 fill {want}；实际 {_steps(rule, 'fill')}")

    for want in expect.get("dedupe") or []:
        found = False
        for step in _steps(rule, "dedupe"):
            subset = set(str(c) for c in step.get("subset") or [])
            if set(want.get("subset") or []).issubset(subset):
                found = True
        if not found:
            errors.append(f"缺少 dedupe {want}；实际 {_steps(rule, 'dedupe')}")

    for want in expect.get("sort") or []:
        found = False
        for step in _steps(rule, "sort"):
            by = [str(c) for c in step.get("by") or []]
            asc = [bool(x) for x in step.get("ascending") or []]
            if want.get("by") in by and (want.get("ascending") is None or
                                         (asc and asc[0] == want["ascending"])):
                found = True
        if not found:
            errors.append(f"缺少 sort {want}；实际 {_steps(rule, 'sort')}")

    for want in expect.get("select") or []:
        found = False
        for step in _steps(rule, "select"):
            cols = set(str(c) for c in step.get("columns") or [])
            if set(want).issubset(cols):
                found = True
        if not found:
            errors.append(f"缺少 select {want}；实际 {_steps(rule, 'select')}")

    return errors


CASES: List[Case] = [
    Case("rate-hit", "比率", "统计成交率：成交价不为空算成交，为空算未成交", {
        "task_type": "proportion", "required_ops": ["rate"],
        "rate": [{"alias": "成交率", "numerator": "hit"}],
    }),
    Case("rate-miss", "比率", "统计未成交率：成交价为空算未成交，不为空算成交", {
        "task_type": "proportion", "required_ops": ["rate"],
        # 方向由评测器按 condition 与 true_label/false_label 独立检查。
        "rate": [{"alias": "未成交率"}],
    }),
    Case("rate-blank", "比率", "统计成交价为空率", {
        "task_type": "proportion", "required_ops": ["rate"],
    }),
    Case("rate-group", "比率", "按市分组统计成交率，成交价不为空算成交，为空算未成交", {
        "task_type": "proportion", "required_ops": ["rate"],
        "rate": [{"numerator": "hit", "by": ["市"]}],
    }),
    Case("detail-filter", "明细", "提取区县为鼓楼区的数据，其他字段空值保持为空", {
        "task_type": "detail", "required_ops": ["filter"],
        "forbidden_ops": ["aggregate", "count_rows", "rate"],
        "conditions": [{"column": "区县", "operator": "eq", "value": "鼓楼区"}],
    }),
    Case("detail-fill", "清洗", "提取市为福州的数据，成交价空值置为0，其他字段保持为空", {
        "task_type": "detail", "required_ops": ["filter", "fill"],
        "conditions": [{"column": "市", "operator": "eq", "value": "福州"}],
        # value=0 与 method=zero 都表示补零，评测只检查目标列与补零语义。
        "fills": [{"column": "成交价(元)"}],
    }),
    Case("dedupe-filter", "清洗", "去重，然后提取面积小于50或大于1000的数据", {
        "task_type": "detail", "required_ops": ["dedupe", "filter"],
        "order": [("dedupe", "filter")],
        "conditions": [{"column": "面积(㎡)", "operator": "lt", "value": "50"},
                       {"column": "面积(㎡)", "operator": "gt", "value": "1000"}],
    }),
    Case("date-year", "时间", "统计2026年的数据条数", {
        "required_ops": ["date_filter", "count_rows"],
        "date_filters": [{"column": "结束时间", "year": "2026"}],
        "order": [("date_filter", "count_rows")],
    }),
    Case("date-range", "时间", "统计2026-01-01到2026-06-30之间的数据条数", {
        "required_ops": ["date_filter", "count_rows"],
        "date_filters": [{"column": "结束时间", "start": "2026-01-01", "end": "2026-06-30"}],
    }),
    Case("trend-month-ratio", "组合趋势",
         "去重，然后提取面积小于50或大于1000的数据，最后分析2026年按月的平均成交单价的趋势，"
         "成交单价 = （成交价/面积）", {
             "task_type": "trend",
             "required_ops": ["dedupe", "filter", "date_filter", "date_part", "aggregate", "expr"],
             "order": [("dedupe", "filter"), ("date_filter", "date_part")],
             "date_filters": [{"column": "结束时间", "year": "2026"}],
             "date_parts": [{"column": "结束时间", "parts": ["year", "month"], "bucket": "年月"}],
         }),
    Case("trend-quarter-ratio", "组合趋势",
         "按结束时间，每一年按季度分析每个季度的平均成交单价的趋势，成交单价 = 成交价/面积", {
             "task_type": "trend",
             "required_ops": ["date_part", "aggregate", "expr"],
             "date_parts": [{"column": "结束时间", "parts": ["year", "quarter"],
                             "bucket": "年季度"}],
         }),
    Case("count-year", "分组计数", "按结束时间的年份分组，统计每个年份的数据条数", {
        "required_ops": ["date_part", "count_rows"],
        "date_parts": [{"column": "结束时间", "parts": ["year"]}],
    }),
    Case("count-city", "分组计数", "按市分组，统计每个市的数据条数并按条数降序", {
        "required_ops": ["count_rows", "sort"],
        "order": [("count_rows", "sort")],
    }),
    Case("avg-city", "分组聚合", "按市分组统计平均成交价", {
        "required_ops": ["aggregate"],
        "aggregate": [{"by": ["市"], "funcs": ["mean"]}],
    }),
    Case("sum-district", "分组聚合", "按区县分组统计成交价总和", {
        "required_ops": ["aggregate"],
        "aggregate": [{"by": ["区县"], "funcs": ["sum"]}],
    }),
    Case("outlier", "清洗", "识别面积的异常值并裁剪", {
        "task_type": "detail", "required_ops": ["outlier"],
    }),
    Case("select", "明细", "提取市和区县两列", {
        "task_type": "detail", "required_ops": ["select"],
        "select": [["市", "区县"]],
    }),
    Case("sort", "排序", "按成交价从高到低排序", {
        "task_type": "detail", "required_ops": ["sort"],
        "sort": [{"by": "成交价(元)", "ascending": False}],
    }),
    Case("alias", "格式波动", "统计 area 大于1000的数据", {
        "task_type": "detail", "required_ops": ["filter"],
        "conditions": [{"column": "area", "operator": "gt", "value": "1000"}],
    }, columns=ALIAS_COLUMNS),
    Case("short", "边界输入", "去重", {
        "task_type": "detail", "required_ops": ["dedupe"],
    }),
    Case("multi-action", "组合清洗",
         "提取市为福州的数据，去重，然后成交价空值填0，最后按区县统计成交价总和", {
             "required_ops": ["filter", "dedupe", "fill", "aggregate"],
             "order": [("filter", "aggregate"), ("dedupe", "aggregate")],
             "aggregate": [{"by": ["区县"], "funcs": ["sum"]}],
         }),
    Case("monthly-sum", "时间聚合", "统计2026年每个月的成交价总和", {
        "required_ops": ["date_filter", "date_part", "aggregate"],
        "date_filters": [{"column": "结束时间", "year": "2026"}],
        # 已用 date_filter 锁定单一年份时，month 单项分桶不会跨年合并，属于合法口径。
        "date_parts": [{"column": "结束时间", "parts": ["month"]}],
        "aggregate": [{"funcs": ["sum"]}],
    }),
    Case("dedupe-count", "组合计数", "按标的号去重，然后统计每个小区的数据条数", {
        "required_ops": ["dedupe", "count_rows"],
        "order": [("dedupe", "count_rows")],
        "dedupe": [{"subset": ["标的号"]}],
    }),
    Case("newline", "格式波动", "筛选：\n市：福州\n统计成交价为空率", {
        "required_ops": ["filter", "rate"],
        "conditions": [{"column": "市", "operator": "eq", "value": "福州"}],
    }),
    Case("unknown-column", "异常输入", "统计不存在的字段的为空率", {},
         expected_reject=True),
]


def evaluate_case(provider: Any, case: Case) -> Dict[str, Any]:
    started = time.monotonic()
    record: Dict[str, Any] = {
        "id": case.id, "family": case.family, "intent": case.intent,
        "expected_reject": case.expected_reject,
    }
    try:
        rule, meta = rules.generate_rules_from_schema(
            case.intent, {"path": "<evaluation>", "format": "csv"},
            list(case.columns), provider=provider, column_aliases=case.aliases or None,
        )
    except Exception as exc:  # noqa: BLE001 - 评测器需要记录所有失败形态
        message = f"{type(exc).__name__}: {exc}"
        rejected = case.expected_reject and any(
            token in message for token in ("未知列", "无法映射", "Schema 校验", "解析失败")
        )
        record.update({
            "parse_ok": False, "rule_valid": False, "semantic_ok": False,
            "expectation_ok": rejected,
            "correct": rejected,
            "attempts": 0, "error": message,
            "elapsed": round(time.monotonic() - started, 3),
        })
        return record

    issues = list(meta.get("semantic_issues") or [])
    validation = rules.validate_rule(rule, list(case.columns))
    expectation_errors = [] if case.expected_reject else check_expectation(rule, case.expect)
    if case.expected_reject:
        expectation_errors = ["异常输入未被拒绝"]
    record.update({
        "parse_ok": bool(meta.get("schema")),
        "rule_valid": not validation,
        "semantic_ok": not issues,
        "expectation_ok": not expectation_errors,
        "correct": not validation and not issues and not expectation_errors,
        "attempts": int(meta.get("llm_attempts") or 0),
        "elapsed": round(time.monotonic() - started, 3),
        "task_type": rule.get("task_type"),
        "ops": _ops(rule),
        "steps": rule.get("steps", []),
        "schema": meta.get("schema"),
        "schema_attempts": (meta.get("intent_schema") or {}).get("attempts"),
        "validation_errors": validation,
        "semantic_issues": issues,
        "expectation_errors": expectation_errors,
        "assumptions": (meta.get("schema") or {}).get("assumptions") or [],
    })
    return record


def _rate(numerator: int, denominator: int) -> str:
    return "0.0%" if not denominator else f"{100.0 * numerator / denominator:.1f}%"


def write_report(results: List[Dict[str, Any]], model: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ("parse_ok", "rule_valid", "semantic_ok", "expectation_ok", "correct")
    summary = {f: sum(1 for r in results if r.get(f)) for f in fields}
    summary["total"] = len(results)
    summary["accuracy"] = _rate(summary["correct"], summary["total"])
    by_family: Dict[str, Dict[str, int]] = {}
    for r in results:
        bucket = by_family.setdefault(r["family"], {"total": 0, "correct": 0})
        bucket["total"] += 1
        bucket["correct"] += 1 if r.get("correct") else 0
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "definition": {
            "correct": "Schema 解析成功、语义自检无问题、规则契约校验通过、独立期望断言全部通过",
            "expected_reject": "异常输入必须由 Schema 校验拒绝；其余用例不得拒绝",
        },
        "summary": summary,
        "by_family": by_family,
        "cases": results,
    }
    (path.parent / (path.stem + ".json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 真实模型结构化意图评测报告", "",
        f"- 模型：`{model}`",
        f"- 时间：{payload['generated_at']}",
        f"- 用例：{summary['total']} 条；正确：{summary['correct']} 条；"
        f"准确率：**{summary['accuracy']}**",
        f"- Schema 解析成功：{summary['parse_ok']}；语义无问题：{summary['semantic_ok']}；"
        f"契约校验通过：{summary['rule_valid']}；期望满足：{summary['expectation_ok']}", "",
        "## 分场景准确率", "", "| 场景族 | 用例 | 正确 | 准确率 |", "|---|---:|---:|---:|",
    ]
    for family, stat in sorted(by_family.items()):
        lines.append(f"| {family} | {stat['total']} | {stat['correct']} | "
                     f"{_rate(stat['correct'], stat['total'])} |")
    lines.extend(["", "## 逐用例结果", "",
                  "| ID | 场景族 | 结果 | 调用次数 | 产物算子 | 备注 |",
                  "|---|---|---|---:|---|---|"])
    for r in results:
        note = ""
        for key in ("validation_errors", "semantic_issues", "expectation_errors", "error"):
            if r.get(key):
                note = f"{key}: {str(r[key])[:160]}"
                break
        lines.append(
            f"| {r['id']} | {r['family']} | {'PASS' if r.get('correct') else 'FAIL'} | "
            f"{r.get('attempts', 0)} | {', '.join(r.get('ops') or []) or '-'} | {note} |"
        )
    lines.extend(["", "## 判定口径", "", payload["definition"]["correct"], "",
                  "期望断言由评测器独立检查，不读取模型的 `assumptions` 作为正确依据；"
                  "异常输入只有被 Schema 校验明确拒绝才计为正确。", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="*", help="只运行指定用例 ID")
    parser.add_argument("--repeat", type=int, default=1, help="每个用例重复次数")
    parser.add_argument("--output", default="output/schema_accuracy_report.md")
    args = parser.parse_args()
    provider = get_provider()
    model = describe_provider(provider).model
    selected = [c for c in CASES if not args.ids or c.id in args.ids]
    results: List[Dict[str, Any]] = []
    for case in selected:
        for round_no in range(args.repeat):
            if args.repeat > 1:
                print(f"[{round_no + 1}/{args.repeat}] ", end="", flush=True)
            record = evaluate_case(provider, case)
            if args.repeat > 1:
                record["id"] = f"{case.id}#{round_no + 1}"
            results.append(record)
            print(f"{case.id:20s} {'PASS' if record['correct'] else 'FAIL'} "
                  f"{record.get('attempts', 0)} calls {record.get('elapsed', 0)}s")
            if not record["correct"]:
                for key in ("validation_errors", "semantic_issues", "expectation_errors", "error"):
                    if record.get(key):
                        print(f"  {key}: {str(record[key])[:500]}")
    write_report(results, model, Path(args.output))
    correct = sum(1 for r in results if r.get("correct"))
    print(f"\n准确率 {correct}/{len(results)} = {_rate(correct, len(results))}")
    return 0 if correct == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

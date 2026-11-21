# -*- coding: utf-8 -*-
"""多行业场景矩阵验证与方案文档生成。

链路：自然语言描述 → 脚本化 LLM 响应（非法 JSON / 错误规则 / 正确规则）
→ /plan 生成并修复规则 → /execute 执行 → /result 查看 → 独立期望值断言。
产物：output/industry_scenario_report.md、output/industry_scenario_results.json、
output/industry_samples/<dataset>/<variant>.csv。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from tests.industry_datasets import DATASETS, build_frame, write_sample
from tests.industry_harness import (
    check_scenario, generation_check, prepare_scenario, run_scenario,
)
from tests.industry_scenarios import (
    CROSS_REGION_MAP, INDUSTRY_SCENARIOS, cross_industry_contexts,
)

REPORT = Path("output/industry_scenario_report.md")
RESULT_JSON = Path("output/industry_scenario_results.json")
SAMPLES = Path("output/industry_samples")


def _md_table(frame: pd.DataFrame, limit: int = 5) -> str:
    head = frame.head(limit)
    cols = [str(c) for c in head.columns]
    lines = [
        "| " + " | ".join(_cell(c) for c in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for _, row in head.iterrows():
        lines.append("| " + " | ".join(_cell(row[c]) for c in head.columns) + " |")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "NULL"
    return str(value).replace("|", "\\|").replace("\n", " ")[:60]


def _cases() -> List[tuple]:
    cases: List[tuple] = []
    for scenario in INDUSTRY_SCENARIOS:
        if scenario.id == "cross-group-count":
            for dataset_id in CROSS_REGION_MAP:
                cases.append((scenario, "baseline", dataset_id))
        else:
            for variant in scenario.variants:
                cases.append((scenario, variant, None))
    return cases


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    base = SAMPLES / "_runs"

    # 每个行业落一份基线样例 + 一份组合压力样例，便于人工抽查字段/值形态。
    for dataset in DATASETS.values():
        write_sample(build_frame(dataset, "baseline"), SAMPLES / dataset.id / "baseline.csv")
        write_sample(
            build_frame(dataset, "renamed+replaced+shuffled+format_mix"),
            SAMPLES / dataset.id / "stress.csv",
        )

    cross_paths: Dict[str, str] = {}
    cross_ctxs = []
    for dataset_id in CROSS_REGION_MAP:
        ctx = prepare_scenario(
            next(s for s in INDUSTRY_SCENARIOS if s.id == "cross-group-count"),
            "baseline", base, dataset_id=dataset_id,
        )
        cross_paths[dataset_id] = str(ctx.source["path"])

    records: List[Dict[str, Any]] = []
    generation_evidence: Dict[str, Dict[str, Any]] = {}
    for scenario, variant, dataset_id in _cases():
        ctx = prepare_scenario(scenario, variant, base, dataset_id=dataset_id)
        gen = generation_check(ctx)
        record = run_scenario(ctx, base / "out")
        problems = check_scenario(ctx, record)
        if scenario.id not in generation_evidence:
            meta = gen["meta"]
            generation_evidence[scenario.id] = {
                "prompt_family": meta.get("prompt_family"),
                "json_repair": bool(meta.get("json_repair")),
                "repair_rounds": meta.get("repair_rounds"),
                "semantic_issues": meta.get("semantic_issues"),
                "llm_attempts": meta.get("llm_attempts"),
                "final_ops": [s.get("op") for s in gen["rule"].get("steps", []) if isinstance(s, dict)],
            }
        expected = scenario.expected_builder(ctx)
        records.append({
            "scenario": scenario.id,
            "dataset": dataset_id or scenario.dataset_id,
            "variant": variant,
            "scale": ctx.scale,
            "intent": scenario.intent,
            "expected": expected,
            "ops": record.get("ops"),
            "task_type": (record.get("summary") or {}).get("task", {}).get("task_type"),
            "run_dir": record.get("run_dir"),
            "problems": problems,
            "pass": not problems,
        })
        print(f"{scenario.id:22s} {variant:35s} {'PASS' if not problems else 'FAIL ' + str(problems[:2])}")

    passed = sum(1 for r in records if r["pass"])
    RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
    RESULT_JSON.write_text(json.dumps({
        "total": len(records), "passed": passed,
        "generation": generation_evidence, "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: List[str] = [
        "# 多行业数据集与场景化组合测试方案 / 验证报告",
        "",
        f"验证链路：**自然语言描述 → 脚本化 LLM 响应 → `/plan` 生成规则 → "
        f"`/execute` 执行 → `/result` 查看 → 独立期望值断言**。"
        f"共 {len(records)} 个用例，通过 {passed} 个。",
        "",
        "脚本化模型按固定顺序返回：①非法 JSON（验证解析失败回灌）；"
        "②语义错误规则（验证自检/修复）；③正确规则。测试从未把答案规则直接提交给 `/execute`。",
        "",
        "## 一、数据集清单",
        "",
        "| 行业 | 数据集 | 典型字段 | 规模样本 | 异常/边界/缺失/格式特征 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for ds in DATASETS.values():
        lines.append(
            f"| {ds.industry} | `{ds.id}` · {ds.title} | {', '.join(ds.columns)} | "
            f"基线 {len(ds.rows)} 行，scaled 变体 {len(ds.rows) * 10} 行 | {ds.sample_notes} |"
        )
    lines += [
        "| 电商×维表 | `product_dim` · 商品维表 | 品类 / 负责人 / 类目编码 | 3 行 | 品类唯一，验证 join 不膨胀 |",
        "",
        "## 二、样例数据（基线前 5 行）",
        "",
    ]
    for ds in DATASETS.values():
        lines += [f"### {ds.industry} · {ds.title}（`{ds.id}`）", "",
                  _md_table(build_frame(ds, "baseline")), "",
                  f"组合压力样例：`output/industry_samples/{ds.id}/stress.csv`"
                  "（字段重命名 + 值替换 + 乱序 + 格式波动）。", ""]

    lines += [
        "## 三、场景组合与预期结果",
        "",
        "| 场景 | 行业 | 数据集 | 描述 | 变体矩阵 | 预期结果（基线） | 关键验证点 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in INDUSTRY_SCENARIOS:
        ctx = prepare_scenario(scenario, "baseline", base / "expect")
        lines.append(
            f"| `{scenario.id}` | {scenario.industry} | `{scenario.dataset_id}` | "
            f"{scenario.intent} | {', '.join(scenario.variants)} | "
            f"`{json.dumps(scenario.expected_builder(ctx), ensure_ascii=False)}` | "
            f"{scenario.focus} |"
        )
    lines += [
        "",
        "跨行业复用场景 `cross-group-count` 对六套结构分别执行："
        + "；".join(f"{k}→{v}" for k, v in CROSS_REGION_MAP.items())
        + "。同一描述只靠显式列别名绑定，不依赖任何单一数据样本。",
        "",
        "## 四、规则生成证据（从描述开始）",
        "",
        "| 场景 | prompt_family | JSON 回灌 | 语义修复轮次 | LLM 调用次数 | 修复后问题 | 最终算子链 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for scenario_id, ev in generation_evidence.items():
        lines.append(
            f"| `{scenario_id}` | {ev['prompt_family']} | {'是' if ev['json_repair'] else '否'} | "
            f"{ev['repair_rounds']} | {ev['llm_attempts']} | "
            f"{ev['semantic_issues'] or '无'} | {' → '.join(ev['final_ops'])} |"
        )
    lines += [
        "",
        "## 五、执行结果明细",
        "",
        "| 场景 | 数据集 | 变体 | 规模 | task_type | 算子链 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        lines.append(
            f"| `{record['scenario']}` | `{record['dataset']}` | {record['variant']} | "
            f"{record['scale']}× | {record['task_type']} | "
            f"{' → '.join(record['ops'] or [])} | {'PASS' if record['pass'] else 'FAIL: ' + str(record['problems'])} |"
        )

    lines += [
        "",
        "## 六、关键验证点",
        "",
        "1. **生成层**：非法 JSON 必须触发错误原文回灌；语义错误规则必须被评测器发现并修复；"
        "最终规则不得保留模型最初的错误算子链。",
        "2. **字段变更**：`renamed` 变体把业务列改成英文，只有显式 `column_aliases` 生效；"
        "组合压力用例同时叠加重命名、值替换、乱序与格式波动。",
        "3. **数据替换**：支付状态、质检结果、签收状态、违约标记等同义词统一到规范值，"
        "比率分子/分母不因写法变化漂移。",
        "4. **顺序调整**：`shuffled` 变体验证分组、排序、去重与聚合不依赖原始行顺序。",
        "5. **格式波动**：金额 `¥1,299.00` / `1.28万`、日期 `2026/02/18` / `2026年06月15日`、"
        "空值 `NULL` / `—` 通过 `convert_unit` / `date_parse` / 保留空值语义处理。",
        "6. **不同规模**：`scaled` 变体把 10 行放大到 100 行，校验比率稳定、计数与金额求和按规模放大。",
        "7. **行业结构差异**：订单、授信、就诊、运单、成绩、批次六类主键与分组维度完全不同，"
        "同一套原语与评测器均能稳定产出可回答问题的最终表。",
        "8. **多源关联**：电商订单表关联商品维表，验证别名引用、关联键对齐与不膨胀。",
        "",
        "## 七、本轮发现并修复的根因",
        "",
        "| 现象 | 根因 | 修复 |",
        "| --- | --- | --- |",
        "| 字段重命名后物流趋势选错时间列 | `_workflow_date_column` 只数「列名含时间」的个数，"
        "`shipped_at + 签收时间` 时误绑签收时间 | 显式别名 > 完整列名 > 通用兜底；"
        "`_workflow_analysis` 传入 aliases |",
        "| 多源 GMV 正确规则被语义评测判为未知列 | `rule_eval._column_refs` 把 join 的 `right` "
        "数据源别名当列引用 | join/union/anti_join/semi_join 的 `right` 跳过列校验 |",
        "",
        "## 八、复现方式",
        "",
        "```bash",
        "python verify_industry_matrix.py",
        "pytest -q tests/test_industry_scenarios.py",
        "```",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"REPORT {REPORT} ({passed}/{len(records)} passed)")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

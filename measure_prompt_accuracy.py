#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实在线模型度量「生成即正确」比例（不经过意图编译兜底）。

用法：python measure_prompt_accuracy.py
报告：output/prompt_quality_report.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import rules
from llm_provider import get_provider, parse_json_response
from ops.rule_eval import evaluate_rule, format_issues
from tests.prompt_scenarios import ALIASES, COLUMNS, SCENARIOS, SOURCE


def main() -> int:
    provider = get_provider()
    results = []
    started = time.monotonic()
    for idx, scenario in enumerate(SCENARIOS, 1):
        intent = scenario["intent"]
        system, user = rules.build_rule_prompt(
            intent, SOURCE, COLUMNS,
            column_aliases={k: v for k, v in ALIASES.items() if v},
        )
        item: dict = {
            "id": scenario["id"], "family": scenario["family"], "intent": intent,
        }
        try:
            raw = provider.complete(system, user)
            candidate = parse_json_response(raw)
            rule = rules.normalize_rule(candidate, SOURCE, intent)
            errors = rules.validate_rule(rule, COLUMNS)
            if errors:
                rule, _ = rules.sanitize_rule(rule, COLUMNS)
                errors = rules.validate_rule(rule, COLUMNS)
            issues = evaluate_rule(rule, intent, COLUMNS, ALIASES)
            item["ops"] = [s.get("op") for s in rule.get("steps", []) if isinstance(s, dict)]
            item["task_type"] = rule.get("task_type")
            item["issues"] = format_issues(issues)
            item["structural_errors"] = errors
            item["ok"] = not issues and not errors
        except Exception as exc:  # noqa: BLE001 - 度量脚本需要记录单场景失败
            item["ok"] = False
            item["issues"] = [f"{type(exc).__name__}: {exc}"]
        results.append(item)
        print(f"[{idx:02d}/{len(SCENARIOS)}] {item['id']} "
              f"{'OK' if item['ok'] else 'FAIL'} {item.get('issues', [])[:1]}", flush=True)
        time.sleep(0.15)

    correct = sum(1 for r in results if r["ok"])
    total = len(results)
    summary = {
        "provider": getattr(provider, "model", type(provider).__name__),
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4),
        "elapsed": round(time.monotonic() - started, 1),
        "by_family": {},
        "results": results,
    }
    for scenario in SCENARIOS:
        family = scenario["family"]
        subset = [r for r in results if r["family"] == family]
        summary["by_family"][family] = {
            "total": len(subset),
            "correct": sum(1 for r in subset if r["ok"]),
        }
    out = Path("output") / "prompt_quality_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                     ensure_ascii=False, indent=2))
    print("FAILURES:")
    for item in results:
        if not item["ok"]:
            print(f"- {item['id']}: {item.get('issues') or item.get('structural_errors')}")
    return 0 if summary["accuracy"] >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""规则结构定点修复。

提示词已经能稳定生成 90%+ 的正确规则；本模块只处理在线模型反复出现的
一种结构错误：把 ``date_filter`` 当成 ``filter.conditions`` 的条件项，
或把日期条件写成普通的 eq/between。修复是纯结构变换，不猜业务语义。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_DATE_OPERATORS = frozenset({
    "eq", "ne", "==", "=", "!=", "between", "gt", "gte", "lt", "lte", "in",
})


def _date_like(column: Any) -> bool:
    return bool(re.search(r"(时间|日期|date|time|年份)", str(column or ""), re.I))


def _is_date_value(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return any(_is_date_value(v) for v in value)
    return bool(re.search(r"\d{4}(-\d{1,2}-\d{1,2})?", str(value or "")))


def repair_date_steps(rule: Dict[str, Any], intent: str) -> Tuple[Dict[str, Any], bool]:
    """把嵌套/错写的日期条件提升为顶层 date_filter 步骤。

    返回 (新规则, 是否发生修改)。不修改入参。
    """
    steps = rule.get("steps")
    if not isinstance(steps, list):
        return rule, False
    new_steps: List[Dict[str, Any]] = []
    date_step: Optional[Dict[str, Any]] = None
    changed = False

    for step in steps:
        if not isinstance(step, dict):
            new_steps.append(step)
            continue
        if step.get("op") == "date_filter":
            date_step = dict(step)
            new_steps.append(date_step)
            continue
        if step.get("op") != "filter":
            new_steps.append(step)
            continue

        conditions = step.get("conditions")
        if not isinstance(conditions, list):
            new_steps.append(step)
            continue
        kept: List[Any] = []
        new_filter = dict(step)
        for cond in conditions:
            if not isinstance(cond, dict):
                kept.append(cond)
                continue
            if str(cond.get("op", "")) == "date_filter":
                extracted = {k: v for k, v in cond.items() if k != "op"}
                date_step = extracted
                changed = True
                continue
            operator = str(cond.get("operator", "")).lower()
            if _date_like(cond.get("column")) and operator in _DATE_OPERATORS:
                value = cond.get("value")
                if _is_date_value(value):
                    if operator == "between" and isinstance(value, (list, tuple)) and len(value) >= 2:
                        date_step = {
                            "op": "date_filter", "column": cond.get("column"),
                            "start": value[0], "end": value[1],
                        }
                    elif re.fullmatch(r"\d{4}", str(value)):
                        date_step = {
                            "op": "date_filter", "column": cond.get("column"),
                            "year": int(value),
                        }
                    changed = True
                    continue
            kept.append(cond)
        new_filter["conditions"] = kept
        new_steps.append(new_filter)

    # 描述里明确给了日期，但规则里没有有效的 date_filter：按描述补一步
    if date_step is None:
        dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", intent)
        year = re.search(r"(\d{4})\s*年", intent)
        date_column = None
        for step in new_steps:
            if isinstance(step, dict) and _date_like(step.get("column")):
                date_column = step.get("column")
                break
        if dates and len(dates) >= 2:
            date_step = {
                "op": "date_filter", "column": date_column or "结束时间",
                "start": dates[0], "end": dates[1],
            }
            changed = True
        elif year:
            date_step = {
                "op": "date_filter", "column": date_column or "结束时间",
                "year": int(year.group(1)),
            }
            changed = True

    if date_step:
        date_step.setdefault("op", "date_filter")
        date_step.setdefault("column", "结束时间")
        new_steps.insert(0, date_step)

    if not changed:
        return rule, False
    out = dict(rule)
    out["steps"] = new_steps
    return out, True


__all__ = ["repair_date_steps"]

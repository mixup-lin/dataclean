# -*- coding: utf-8 -*-
"""条件模型：单条件 / 复合条件的掩码构造、可读描述与校验。

filter 与 pct(condition) 共用同一套条件语义，因此单独成层，
避免"筛选"和"占比"各自实现一套条件判断导致口径不一致。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import pandas as pd

from ops.constants import FILTER_OPERATORS, LOGIC_OPERATORS, as_list
from ops.errors import RuleValidationError
from ops.primitives import numeric

# 日期文本里「年份后紧跟分隔符」的形态：2026-03-08 / 2026/3/8 / 2026年3月8日
_YEAR_TEXT = re.compile(r"((?:19|20)\d{2})[-/年.]")
_YEAR_VALUE = re.compile(r"^(?:19|20)\d{2}$")

_SYMBOLS = {
    "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
    "contains": "包含", "not_contains": "不包含",
    "startswith": "以…开头", "endswith": "以…结尾", "regex": "匹配",
    "in": "属于", "not_in": "不属于", "between": "介于",
    "notna": "非空", "isna": "为空",
}


# ------------------------------------------------------------
# 掩码构造
# ------------------------------------------------------------
def _year_of(s: pd.Series) -> pd.Series:
    """从日期文本里抽取年份（字符串形态，规避时区与 pandas 版本差异）。

    只认「4 位年份 + 日期分隔符」，因此 ``建成年份=2017`` 这类纯数字列
    不会被误判为日期列，语义改写不会误伤普通数值/类别列。
    """
    return s.astype("string").str.extract(_YEAR_TEXT, expand=False)


def _year_equality_mask(
    s: pd.Series, operator: str, value: Any
) -> Optional[pd.Series]:
    """日期列 ``eq/ne`` 一个 4 位年份时，按「年份相等」解释而非字符串全等。

    需求「结束时间在 2026 年」常被写成 ``{"operator":"eq","value":"2026"}``，
    但日期列实际值是 ``2026-03-08T10:00:00+08:00``，字符串全等恒为 False，
    会把整张表筛成 0 行。这里与 ``between`` 的日期智能比较保持同一设计：
    仅当该列 >=60% 的值确实是「年份+分隔符」的日期文本时才启用。
    """
    raw = str(value).strip() if value is not None else ""
    if not _YEAR_VALUE.match(raw):
        return None
    years = _year_of(s)
    valid = years.notna()
    if len(years) == 0 or float(valid.mean()) < 0.6:
        return None
    mask = (years == raw).fillna(False)
    return ~mask if operator == "ne" else mask


def condition_mask(df: pd.DataFrame, cond: Dict[str, Any]) -> pd.Series:
    """单条件 → 布尔掩码。"""
    column = str(cond.get("column", ""))
    if column not in df.columns:
        raise RuleValidationError([f"条件列不存在：{column}"])
    operator = str(cond.get("operator", ""))
    value = cond.get("value")
    s = df[column]

    if operator == "notna":
        return s.notna()
    if operator == "isna":
        return s.isna()

    if operator in ("contains", "not_contains", "startswith", "endswith", "regex"):
        text = s.astype("string").fillna("")
        pattern = str(value)
        if operator == "startswith":
            mask = text.str.startswith(pattern, na=False)
        elif operator == "endswith":
            mask = text.str.endswith(pattern, na=False)
        elif operator == "regex":
            mask = text.str.contains(pattern, case=False, na=False, regex=True)
        else:
            mask = text.str.contains(pattern, case=False, na=False, regex=False)
        mask = mask.fillna(False)
        return ~mask if operator == "not_contains" else mask

    if operator in ("in", "not_in"):
        values = value if isinstance(value, list) else [value]
        try:
            targets = {float(v) for v in values}
            mask = numeric(s).isin(targets)
        except (TypeError, ValueError):
            mask = s.astype(str).isin([str(v) for v in values])
        return mask.fillna(False) if operator == "in" else ~mask.fillna(False)

    if operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise RuleValidationError(["between 需要 [下界, 上界]"])
        try:
            num = numeric(s)
            return num.between(float(value[0]), float(value[1]), inclusive="both")
        except (TypeError, ValueError):
            pass
        # 日期/时间列：把边界解析为日期，避免字符串比较越界
        try:
            dt = pd.to_datetime(s, errors="coerce")
            if bool(dt.notna().any()):
                lo = pd.to_datetime(str(value[0]), errors="coerce")
                hi = pd.to_datetime(str(value[1]), errors="coerce")
                if lo is not None and hi is not None:
                    return dt.between(lo, hi, inclusive="both").fillna(False)
        except Exception:  # noqa: BLE001 - 解析失败则退回字符串比较
            pass
        return s.astype(str).between(str(value[0]), str(value[1]))

    ops = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
        "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
    }
    if operator not in ops:
        raise RuleValidationError([f"未知条件算子：{operator}"])
    if operator in ("eq", "ne"):
        year_mask = _year_equality_mask(s, operator, value)
        if year_mask is not None:
            return year_mask
    try:
        target: Any = float(value)
        comparable: Any = numeric(s)
    except (TypeError, ValueError):
        target = value
        comparable = s.astype(str)
    return ops[operator](comparable, target).fillna(False)


def build_mask(df: pd.DataFrame, node: Dict[str, Any]) -> pd.Series:
    """构造布尔掩码：支持单条件 dict，或复合条件 ``{"conditions": [...], "logic"}``。"""
    if isinstance(node, dict) and isinstance(node.get("conditions"), list) and node["conditions"]:
        logic = str(node.get("logic", "and")).lower()
        mask: Optional[pd.Series] = None
        for cond in node["conditions"]:
            if not isinstance(cond, dict):
                raise RuleValidationError(["conditions 项必须是对象"])
            part = build_mask(df, cond)
            if mask is None:
                mask = part
            else:
                mask = (mask & part) if logic != "or" else (mask | part)
        return mask if mask is not None else pd.Series(True, index=df.index)
    return condition_mask(df, node)


def describe_condition(cond: Dict[str, Any]) -> str:
    """单条件或复合条件的可读描述；复合条件用「条件1 且/或 条件2」串联。"""
    if isinstance(cond.get("conditions"), list) and cond["conditions"]:
        logic = "或" if str(cond.get("logic", "and")).lower() == "or" else "且"
        parts = [describe_condition(c) for c in cond["conditions"] if isinstance(c, dict)]
        return " ".join(parts) if len(parts) <= 1 else f"({logic.join(parts)})"
    column = cond.get("column")
    operator = str(cond.get("operator"))
    label = _SYMBOLS.get(operator, operator)
    if operator in ("notna", "isna"):
        return f"{column} {label}"
    return f"{column} {label} {cond.get('value')}"


def single_condition(step: Dict[str, Any]) -> Dict[str, Any]:
    """把 filter 顶层的单条件字段整理成条件 dict。"""
    return {
        "column": step.get("column"),
        "operator": step.get("operator"),
        "value": step.get("value"),
    }


# ------------------------------------------------------------
# 校验
# ------------------------------------------------------------
def check_condition(
    cond: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    column = cond.get("column")
    if not isinstance(column, str) or not column:
        errors.append(f"{tag}.column 必须是非空字符串")
    elif known and column not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {column!r}")
    operator = cond.get("operator")
    if operator not in FILTER_OPERATORS:
        errors.append(f"{tag}.operator 非法，可选 {list(FILTER_OPERATORS)}")
    elif operator not in ("notna", "isna") and "value" not in cond:
        errors.append(f"{tag} 缺少字段 value")


def check_condition_or_compound(
    cond: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    """校验单条件或复合条件（conditions + logic）。"""
    if isinstance(cond.get("conditions"), list) and cond["conditions"]:
        if cond.get("logic", "and") not in LOGIC_OPERATORS:
            errors.append(f"{tag}.logic 必须是 and 或 or")
        for i, c in enumerate(cond["conditions"]):
            if not isinstance(c, dict):
                errors.append(f"{tag}.conditions[{i}] 必须是对象")
                continue
            check_condition(c, f"{tag}.conditions[{i}]", known, errors)
    else:
        check_condition(cond, tag, known, errors)


def condition_columns(node: Any) -> List[str]:
    """递归收集一个步骤里所有「条件型」字典引用的列（保序去重）。

    覆盖 filter 顶层单条件 / conditions 数组、assign.rules[].condition、
    pct.condition、rate.condition、assert.checks、having、
    cross_column_filter 的 left/right 等形态。执行层据此在清洗后还原这些列的
    原始空值，新增带条件的算子无需再逐个声明 ``preserve_refs``。
    """
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("operator"), str):
                for key in ("column", "left", "right"):
                    col = value.get(key)
                    if isinstance(col, str) and col.strip():
                        found.append(col.strip())
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return list(dict.fromkeys(found))


def check_filter(
    step: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    conditions = step.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, list) or not conditions:
            errors.append(f"{tag}.conditions 必须是非空数组")
            return
        if step.get("logic", "and") not in LOGIC_OPERATORS:
            errors.append(f"{tag}.logic 必须是 and 或 or")
        for i, cond in enumerate(conditions):
            if not isinstance(cond, dict):
                errors.append(f"{tag}.conditions[{i}] 必须是对象")
                continue
            check_condition(cond, f"{tag}.conditions[{i}]", known, errors)
    else:
        check_condition(step, tag, known, errors)


__all__ = [
    "condition_mask",
    "build_mask",
    "describe_condition",
    "single_condition",
    "check_condition",
    "check_condition_or_compound",
    "check_filter",
    "condition_columns",
]

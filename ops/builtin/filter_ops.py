# -*- coding: utf-8 -*-
"""筛选增强算子：date_filter / cross_column_filter。

- ``date_filter`` 一步完成「结束时间在 2026 年」这类条件，不再需要
  ``date_part`` 派生中间列，避免派生列进入列集合后污染分组维度。
- ``cross_column_filter`` 支持「起拍价 > 评估价」等跨列业务规则。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import CROSS_COLUMN_OPERATORS, as_list
from ops.datetime_utils import to_datetime_series
from ops.errors import RuleValidationError
from ops.primitives import numeric
from ops.registry import OpSpec, ParamSpec, register_op

# 保留模块内别名，既有调用点无需改动。
_to_datetime = to_datetime_series


def _align_bound(value: str, dt: pd.Series) -> pd.Timestamp:
    """把日期边界对齐到 dt 的时区。

    ``lots.csv`` 的结束时间是 ``2026-03-08T10:00:00+08:00``，解析后为
    tz-aware ``datetime64[us, UTC+08:00]``；而 ``start/end`` 是 tz-naive
    ``Timestamp``，pandas 3.x 直接比较会抛
    ``Invalid comparison between dtype=... and Timestamp``。此处统一时区，
    且不做 UTC 转换——避免跨年边界（元旦）被换算到上一年。
    """
    ts = pd.to_datetime(value, errors="coerce")
    tz = getattr(dt.dtype, "tz", None)
    if tz is not None and getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize(tz)
    elif tz is None and getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    return ts


def _as_int_list(value: Any) -> List[int]:
    out: List[int] = []
    for item in as_list(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


_DATE_KEYS = ("year", "quarter", "month", "start", "end", "recent_days")


def apply_date_filter(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    column = str(step.get("column", ""))
    if column not in df.columns:
        raise RuleValidationError([f"date_filter 列不存在：{column}"])
    dt = _to_datetime(df[column])
    mask = dt.notna()
    parts: List[str] = []

    years = _as_int_list(step.get("year"))
    if years:
        mask &= dt.dt.year.isin(years)
        parts.append("年∈" + "/".join(str(y) for y in years))
    quarters = _as_int_list(step.get("quarter"))
    if quarters:
        mask &= dt.dt.quarter.isin(quarters)
        parts.append("季度∈" + "/".join(str(q) for q in quarters))
    months = _as_int_list(step.get("month"))
    if months:
        mask &= dt.dt.month.isin(months)
        parts.append("月∈" + "/".join(str(m) for m in months))
    if step.get("start") is not None:
        start = _align_bound(str(step["start"]), dt)
        if pd.isna(start):
            raise RuleValidationError(["date_filter.start 不是合法日期"])
        mask &= dt >= start
        parts.append(f"≥{start.date()}")
    if step.get("end") is not None:
        # 日期字符串写到日为止时，按当天 23:59:59 包容，避免漏掉当天数据
        raw = str(step["end"])
        end = _align_bound(raw, dt)
        if pd.isna(end):
            raise RuleValidationError(["date_filter.end 不是合法日期"])
        if len(raw.strip()) <= 10 and "T" not in raw and " " not in raw:
            end = end + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        mask &= dt <= end
        parts.append(f"≤{end.date()}")
    recent = step.get("recent_days")
    if recent is not None:
        days = int(recent)
        boundary = _align_bound(
            (pd.Timestamp.now().normalize() - pd.Timedelta(days=days)).strftime("%Y-%m-%d"),
            dt,
        )
        mask &= dt >= boundary
        parts.append(f"近 {days} 天")

    if step.get("keep_na"):
        mask |= dt.isna()
        parts.append("保留空日期")
    mask = mask.fillna(False)
    return df[mask].copy(), (
        f"日期筛选 {column}（{' 且 '.join(parts) or '无有效条件'}）"
        f"→ 保留 {int(mask.sum())}/{len(df)} 行"
    )


def _validate_date_filter(step, known, tag, errors) -> None:
    column = step.get("column")
    if not isinstance(column, str) or not column:
        errors.append(f"{tag}.column 必须是非空字符串")
    elif known and column not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {column!r}")
    if not any(step.get(k) is not None for k in _DATE_KEYS):
        errors.append(f"{tag} 至少需要一个筛选条件（year/quarter/month/start/end/recent_days）")
    for key in ("quarter", "month"):
        if step.get(key) is not None:
            vals = _as_int_list(step[key])
            upper = 4 if key == "quarter" else 12
            if not vals or any(v < 1 or v > upper for v in vals):
                errors.append(f"{tag}.{key} 必须是 1~{upper} 的整数")
    if step.get("year") is not None and not _as_int_list(step["year"]):
        errors.append(f"{tag}.year 必须是年份整数或数组")
    if step.get("recent_days") is not None:
        val = step["recent_days"]
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
            errors.append(f"{tag}.recent_days 必须是正数")
    for key in ("start", "end"):
        if step.get(key) is not None and not isinstance(step[key], str):
            errors.append(f"{tag}.{key} 必须是日期字符串")


def _date_filter_refs(step: Dict[str, Any]) -> List[str]:
    return [step.get("column")]


def apply_cross_column_filter(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    left = str(step.get("left", ""))
    right = str(step.get("right", ""))
    for col in (left, right):
        if col not in df.columns:
            raise RuleValidationError([f"cross_column_filter 列不存在：{col}"])
    operator = str(step.get("operator") or "gt")
    a, b = numeric(df[left]), numeric(df[right])
    if a.notna().any() and b.notna().any():
        lhs, rhs = a, b
    else:
        lhs, rhs = df[left].astype(str), df[right].astype(str)
    ops: Dict[str, Any] = {
        "gt": lambda x, y: x > y, "gte": lambda x, y: x >= y,
        "lt": lambda x, y: x < y, "lte": lambda x, y: x <= y,
        "eq": lambda x, y: x == y, "ne": lambda x, y: x != y,
    }
    mask = ops[operator](lhs, rhs)
    tol = step.get("tolerance")
    if tol is not None and operator in ("eq", "ne"):
        diff = (a - b).abs()
        eq = diff <= float(tol)
        mask = eq if operator == "eq" else ~eq
    mask = mask.fillna(False)
    if step.get("keep_na"):
        mask |= a.isna() | b.isna()
    return df[mask].copy(), (
        f"跨列筛选 {left} {operator} {right} → 保留 {int(mask.sum())}/{len(df)} 行"
    )


def _validate_cross_column_filter(step, known, tag, errors) -> None:
    for key in ("left", "right"):
        val = step.get(key)
        if not isinstance(val, str) or not val:
            errors.append(f"{tag}.{key} 必须是列名")
        elif known and val not in known:
            errors.append(f"{tag}.{key} 引用了不存在的列 {val!r}")
    if step.get("operator") is not None and step["operator"] not in CROSS_COLUMN_OPERATORS:
        errors.append(f"{tag}.operator 非法，可选 {list(CROSS_COLUMN_OPERATORS)}")


def _cross_refs(step: Dict[str, Any]) -> List[str]:
    return [step.get("left"), step.get("right")]


register_op(OpSpec(
    name="date_filter", layer=1, role="filter", execute=apply_date_filter,
    validate=_validate_date_filter, refs=_date_filter_refs,
    preserve_refs=_date_filter_refs,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("year", None), ParamSpec("quarter", None),
            ParamSpec("month", None), ParamSpec("start", str),
            ParamSpec("end", str), ParamSpec("recent_days", (int, float)),
            ParamSpec("keep_na", bool)),
    summary="按年/季度/月/日期区间/相对时间直接筛选日期列（无需派生中间列）",
    prompt_example={"op": "date_filter", "column": "<日期列>", "year": 2026},
))
register_op(OpSpec(
    name="cross_column_filter", layer=1, role="filter", execute=apply_cross_column_filter,
    validate=_validate_cross_column_filter, refs=_cross_refs, preserve_refs=_cross_refs,
    params=(ParamSpec("left", str, required=True, column=True),
            ParamSpec("right", str, required=True, column=True),
            ParamSpec("operator", str, enum=CROSS_COLUMN_OPERATORS)),
    summary="按两列之间的大小/相等关系筛选（业务规则校验）",
    prompt_example={"op": "cross_column_filter", "left": "<起拍价>", "right": "<评估价>", "operator": "gt"},
))

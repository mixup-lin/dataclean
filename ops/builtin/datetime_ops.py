# -*- coding: utf-8 -*-
"""时间算子：date_diff（日期差 / 时长 / 账龄）。

date_part 负责把时间拆成分量用于分组，date_diff 负责两个时间点之间的距离，
覆盖「持有天数」「成交周期」「逾期月数」这类常见需求。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import DATE_DIFF_UNITS
from ops.errors import RuleValidationError
from ops.primitives import finite, require_columns
from ops.registry import OpSpec, ParamSpec, register_op


def _to_datetime(series: pd.Series) -> pd.Series:
    """统一按 UTC 解析：带时区与不带时区的列相减不会因 tz 不一致而报错。"""
    return pd.to_datetime(series, errors="coerce", format="mixed", utc=True)


def apply_date_diff(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    left = str(step.get("left"))
    right = str(step.get("right"))
    require_columns(df, [left, right], "date_diff")
    unit = str(step.get("unit") or "day")
    if unit not in DATE_DIFF_UNITS:
        raise RuleValidationError([f"未知时间单位：{unit}"])
    default_into = f"{left}_距{right}" if unit == "day" else f"{left}_距{right}_{unit}"
    into = step.get("into") or default_into
    start, end = _to_datetime(df[left]), _to_datetime(df[right])
    delta = start - end
    if unit in ("day", "hour", "minute"):
        seconds = delta.dt.total_seconds()
        values = seconds / {"day": 86400.0, "hour": 3600.0, "minute": 60.0}[unit]
    else:
        months = (start.dt.year - end.dt.year) * 12 + (start.dt.month - end.dt.month)
        values = months if unit == "month" else months / 12.0
    frame = df.copy()
    frame[into] = finite(values).round(2)
    valid = int(frame[into].notna().sum())
    return frame, f"日期差 {left} - {right} → {into}（{unit}，{valid} 行可解析）"


def _refs(step: Dict[str, Any]) -> List[str]:
    return [step.get("left"), step.get("right")]


def _outputs(step: Dict[str, Any]) -> List[str]:
    into = step.get("into")
    if isinstance(into, str) and into:
        return [into]
    left, right = step.get("left"), step.get("right")
    if not (isinstance(left, str) and isinstance(right, str) and left and right):
        return []
    unit = str(step.get("unit") or "day")
    return [f"{left}_距{right}"] if unit == "day" else [f"{left}_距{right}_{unit}"]


def _validate(step, known, tag, errors) -> None:
    if not isinstance(step.get("left"), str) or not step.get("left"):
        errors.append(f"{tag}.left 必须是时间列名")
    if not isinstance(step.get("right"), str) or not step.get("right"):
        errors.append(f"{tag}.right 必须是时间列名")
    if step.get("unit") is not None and step["unit"] not in DATE_DIFF_UNITS:
        errors.append(f"{tag}.unit 非法，可选 {list(DATE_DIFF_UNITS)}")


register_op(OpSpec(
    name="date_diff", layer=1, role="transform", execute=apply_date_diff, validate=_validate,
    params=(ParamSpec("left", str, required=True, column=True),
            ParamSpec("right", str, required=True, column=True),
            ParamSpec("unit", str, enum=DATE_DIFF_UNITS)),
    refs=_refs, outputs=_outputs, summary="两个时间列的差（天/小时/分钟/月/年）",
    prompt_example={"op": "date_diff", "left": "<结束时间>", "right": "<开始时间>", "unit": "day", "into": "<新列>"},
))

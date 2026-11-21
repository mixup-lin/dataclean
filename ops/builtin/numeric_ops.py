# -*- coding: utf-8 -*-
"""数值算子：clip / normalize / outlier / cumsum / describe。

这些是「清洗后到分析前」最常用的一层数值加工：截断极值、标准化、
异常值标记/裁剪/剔除、累计求和以及整体统计概览。
全部实现为纯函数式步骤，不改动清洗阶段的全局配置。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import (
    NORMALIZE_METHODS, OUTLIER_ACTIONS, OUTLIER_METHODS, as_list,
)
from ops.errors import RuleValidationError
from ops.primitives import existing_cols, finite, numeric, require_columns
from ops.registry import OpSpec, ParamSpec, register_op

# describe 产出的固定统计列，供后续步骤引用
DESCRIBE_STATS = ("非空数", "均值", "标准差", "最小", "中位数", "最大")


def _target_cols(df: pd.DataFrame, step: Dict[str, Any], tag: str) -> List[str]:
    cols = existing_cols(df, as_list(step.get("columns")) + as_list(step.get("column")))
    if not cols:
        raise RuleValidationError([f"{tag} 未匹配到有效列"])
    return cols


def _bounds(step: Dict[str, Any]) -> Tuple[Any, Any]:
    lower, upper = step.get("lower"), step.get("upper")
    if lower is None and upper is None:
        raise RuleValidationError(["clip 至少需要 lower 或 upper"])
    if lower is not None and upper is not None and float(lower) > float(upper):
        raise RuleValidationError(["clip 的 lower 不能大于 upper"])
    return lower, upper


def apply_clip(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    cols = _target_cols(df, step, "clip")
    lower, upper = _bounds(step)
    frame = df.copy()
    for col in cols:
        frame[col] = finite(numeric(frame[col]).clip(lower=lower, upper=upper))
    return frame, f"截断 {','.join(cols)} 到 [{lower}, {upper}]"


def apply_normalize(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    column = str(step.get("column"))
    require_columns(df, [column], "normalize")
    method = str(step.get("method") or "minmax")
    into = step.get("into") or f"{column}_{method}"
    num = numeric(df[column])
    frame = df.copy()
    if method == "minmax":
        lo, hi = num.min(), num.max()
        span = float(hi - lo) if pd.notna(lo) and pd.notna(hi) else 0.0
        frame[into] = finite((num - lo) / span) if span else 0.0
    elif method == "zscore":
        std = float(num.std(ddof=0)) if num.notna().any() else 0.0
        frame[into] = finite((num - num.mean()) / std) if std else 0.0
    elif method == "rank_pct":
        frame[into] = num.rank(pct=True)
    else:
        raise RuleValidationError([f"未知标准化方式：{method}"])
    return frame, f"标准化 {column} → {into}（{method}）"


def _outlier_mask(num: pd.Series, method: str, threshold: float) -> Tuple[pd.Series, float, float]:
    """返回 (异常掩码, 下界, 上界)；非数值行不算异常。"""
    clean = num.dropna()
    if clean.empty:
        return pd.Series(False, index=num.index), 0.0, 0.0
    if method == "zscore":
        mean, std = float(clean.mean()), float(clean.std(ddof=0))
        if not std:
            return pd.Series(False, index=num.index), 0.0, 0.0
        lower, upper = mean - threshold * std, mean + threshold * std
    else:  # iqr
        q1, q3 = float(clean.quantile(0.25)), float(clean.quantile(0.75))
        iqr = q3 - q1
        if not iqr:
            return pd.Series(False, index=num.index), q1, q3
        lower, upper = q1 - threshold * iqr, q3 + threshold * iqr
    mask = (num < lower) | (num > upper)
    return mask.fillna(False), lower, upper


def apply_outlier(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    column = str(step.get("column"))
    require_columns(df, [column], "outlier")
    method = str(step.get("method") or "iqr")
    action = str(step.get("action") or "flag")
    threshold = step.get("threshold")
    threshold = float(threshold) if threshold not in (None, "") else (
        3.0 if method == "zscore" else 1.5
    )
    num = numeric(df[column])
    mask, lower, upper = _outlier_mask(num, method, threshold)
    count = int(mask.sum())
    frame = df.copy()
    if action == "flag":
        into = step.get("into") or f"{column}_异常"
        frame[into] = mask
        return frame, f"标记 {column} 异常值 {count} 行（{method}，界 [{lower:.4g}, {upper:.4g}]）"
    if action == "clip":
        frame[column] = finite(num.clip(lower=lower, upper=upper))
        return frame, f"裁剪 {column} 异常值 {count} 行（{method}）"
    if action == "remove":
        before = len(frame)
        frame = frame[~mask]
        return frame, f"删除 {column} 异常值 {before - len(frame)} 行（{method}）"
    raise RuleValidationError([f"未知异常处理动作：{action}"])


def apply_cumsum(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    column = str(step.get("column"))
    require_columns(df, [column], "cumsum")
    by = step.get("by")
    into = step.get("into") or f"{column}_累计"
    frame = df.copy()
    num = numeric(frame[column])
    if isinstance(by, str) and by in frame.columns:
        frame[into] = num.groupby(frame[by], dropna=False).cumsum()
        return frame, f"分组累计 {column} → {into}（按 {by}）"
    frame[into] = num.cumsum()
    return frame, f"累计 {column} → {into}"


def apply_describe(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    cols = existing_cols(df, as_list(step.get("columns")) + as_list(step.get("column")))
    if not cols:
        cols = [str(c) for c in df.select_dtypes(include="number").columns]
    if not cols:
        raise RuleValidationError(["describe 需要至少一个数值列"])
    rows: List[Dict[str, Any]] = []
    for col in cols:
        num = numeric(df[col])
        valid = num.dropna()
        rows.append({
            "列": col,
            "非空数": int(valid.size),
            "均值": round(float(valid.mean()), 4) if valid.size else 0.0,
            "标准差": round(float(valid.std(ddof=0)), 4) if valid.size else 0.0,
            "最小": float(valid.min()) if valid.size else 0.0,
            "中位数": round(float(valid.median()), 4) if valid.size else 0.0,
            "最大": float(valid.max()) if valid.size else 0.0,
        })
    return pd.DataFrame(rows), f"统计概览 {len(rows)} 列"


# ------------------------------------------------------------
# 输出列 / 输入列 / 校验
# ------------------------------------------------------------
def _out_normalize(step: Dict[str, Any]) -> List[str]:
    col = step.get("column")
    if not isinstance(col, str) or not col:
        return []
    method = str(step.get("method") or "minmax")
    into = step.get("into") or f"{col}_{method}"
    return [str(into)]


def _out_outlier(step: Dict[str, Any]) -> List[str]:
    if str(step.get("action") or "flag") != "flag":
        return []
    col = step.get("column")
    if not isinstance(col, str) or not col:
        return []
    return [str(step.get("into") or f"{col}_异常")]


def _out_cumsum(step: Dict[str, Any]) -> List[str]:
    col = step.get("column")
    if not isinstance(col, str) or not col:
        return []
    return [str(step.get("into") or f"{col}_累计")]


def _out_describe(step: Dict[str, Any]) -> List[str]:
    return ["列", *DESCRIBE_STATS]


def _refs_by(step: Dict[str, Any]) -> List[str]:
    return [step.get("column")] + as_list(step.get("by"))


def _validate_clip(step, known, tag, errors) -> None:
    if step.get("lower") is None and step.get("upper") is None:
        errors.append(f"{tag} 至少需要 lower 或 upper")


def _validate_normalize(step, known, tag, errors) -> None:
    if step.get("method") is not None and step["method"] not in NORMALIZE_METHODS:
        errors.append(f"{tag}.method 非法，可选 {list(NORMALIZE_METHODS)}")


def _validate_outlier(step, known, tag, errors) -> None:
    if step.get("method") is not None and step["method"] not in OUTLIER_METHODS:
        errors.append(f"{tag}.method 非法，可选 {list(OUTLIER_METHODS)}")
    if step.get("action") is not None and step["action"] not in OUTLIER_ACTIONS:
        errors.append(f"{tag}.action 非法，可选 {list(OUTLIER_ACTIONS)}")
    threshold = step.get("threshold")
    if threshold is not None and (
        isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0
    ):
        errors.append(f"{tag}.threshold 必须是正数")


register_op(OpSpec(
    name="clip", layer=1, role="transform", execute=apply_clip, validate=_validate_clip,
    params=(ParamSpec("column", (str, list), column=True, multi=True),),
    summary="数值截断到 [lower, upper]",
    prompt_example={"op": "clip", "column": "<数值列>", "lower": 0, "upper": 1000000},
))
register_op(OpSpec(
    name="normalize", layer=1, role="transform", execute=apply_normalize, validate=_validate_normalize,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("method", str, enum=NORMALIZE_METHODS)),
    outputs=_out_normalize, summary="数值标准化（minmax / zscore / rank_pct）",
    prompt_example={"op": "normalize", "column": "<数值列>", "method": "minmax", "into": "<新列>"},
))
register_op(OpSpec(
    name="outlier", layer=1, role="transform", execute=apply_outlier, validate=_validate_outlier,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("method", str, enum=OUTLIER_METHODS),
            ParamSpec("action", str, enum=OUTLIER_ACTIONS)),
    outputs=_out_outlier, summary="单列异常值标记 / 裁剪 / 删除",
    prompt_example={"op": "outlier", "column": "<数值列>", "method": "iqr", "action": "flag"},
))
register_op(OpSpec(
    name="cumsum", layer=1, role="transform", execute=apply_cumsum,
    params=(ParamSpec("column", str, required=True, column=True),),
    outputs=_out_cumsum, refs=_refs_by, summary="累计求和（可按分组）",
    prompt_example={"op": "cumsum", "column": "<数值列>", "by": "<可选分组列>", "into": "<新列>"},
))
register_op(OpSpec(
    name="describe", layer=2, role="analysis", execute=apply_describe,
    params=(ParamSpec("columns", list, column=True, multi=True),),
    outputs=_out_describe, summary="数值列统计概览（非空数/均值/标准差/最值）",
    task_hint="summary",
    prompt_example={"op": "describe", "columns": ["<数值列>"]},
))

# -*- coding: utf-8 -*-
"""分析增强算子：corr / having / group_rank / anti_join / semi_join。

补齐「展示层有 correlation / TopN / 对账差集，计算层却无算子」的错配。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import (
    AGG_TOPN_MODES, CORR_METHODS, HAVING_OPERATORS, as_list,
)
from ops.errors import RuleValidationError
from ops.primitives import as_cols, numeric
from ops.registry import OpSpec, ParamSpec, register_op


# ------------------------------------------------------------
# corr
# ------------------------------------------------------------
def apply_corr(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = [c for c in as_cols(step.get("columns")) if c in df.columns]
    if not cols:
        cols = list(df.select_dtypes(include="number").columns)
    # 排除 ID / 常量列：全空或唯一值 ≤1 的列做相关只会得到 NaN
    cols = [c for c in cols if numeric(df[c]).nunique(dropna=True) >= 2]
    if len(cols) < 2:
        raise RuleValidationError(["corr 至少需要两个有变化的数值列"])
    method = str(step.get("method") or "pearson")
    if method not in CORR_METHODS:
        raise RuleValidationError([f"corr.method 非法：{method}"])
    min_periods = step.get("min_periods")
    kwargs = {"min_periods": int(min_periods)} if min_periods is not None else {}
    matrix = df[cols].corr(method=method, **kwargs).round(4).reset_index()
    matrix = matrix.rename(columns={matrix.columns[0]: "指标"})
    return matrix, f"相关性矩阵 {method}（{len(cols)} 个数值列，已排除 ID/常量列）"


def _corr_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("columns"))


def _corr_outputs(step: Dict[str, Any]) -> List[str]:
    return ["指标"] + as_cols(step.get("columns"))


def _validate_corr(step, known, tag, errors) -> None:
    for col in as_cols(step.get("columns")):
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    if step.get("method") is not None and step["method"] not in CORR_METHODS:
        errors.append(f"{tag}.method 非法，可选 {list(CORR_METHODS)}")


# ------------------------------------------------------------
# having
# ------------------------------------------------------------
def apply_having(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    topn = step.get("topn")
    column = str(step.get("column") or "")
    if topn is not None:
        n = int(topn)
        if n < 1:
            raise RuleValidationError(["having.topn 必须是正整数"])
        if column not in df.columns:
            raise RuleValidationError([f"having 列不存在：{column}"])
        mode = str(step.get("mode") or "top")
        out = df.sort_values(column, ascending=(mode == "bottom"), kind="mergesort").head(n)
        return out.copy(), f"聚合后筛选 {column} {mode} {n} → {len(out)} 行"
    if column not in df.columns:
        raise RuleValidationError([f"having 列不存在：{column}"])
    operator = str(step.get("operator") or "gt")
    value = step.get("value")
    target: Any
    try:
        target = float(value)
        series: Any = numeric(df[column])
    except (TypeError, ValueError):
        target = value
        series = df[column].astype(str)
    ops = {
        "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
        "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
        "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    }
    mask = ops[operator](series, target).fillna(False)
    return df[mask].copy(), f"聚合后筛选 {column} {operator} {value} → {int(mask.sum())} 行"


def _validate_having(step, known, tag, errors) -> None:
    col = step.get("column")
    if not isinstance(col, str) or not col:
        errors.append(f"{tag}.column 必须是列名")
    elif known and col not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {col!r}")
    if step.get("operator") is not None and step["operator"] not in HAVING_OPERATORS:
        errors.append(f"{tag}.operator 非法，可选 {list(HAVING_OPERATORS)}")
    if step.get("topn") is None and step.get("value") is None:
        errors.append(f"{tag} 需要 topn 或 value")
    topn = step.get("topn")
    if topn is not None and (not isinstance(topn, int) or isinstance(topn, bool) or topn < 1):
        errors.append(f"{tag}.topn 必须是正整数")
    if step.get("mode") is not None and step["mode"] not in AGG_TOPN_MODES:
        errors.append(f"{tag}.mode 非法，可选 {list(AGG_TOPN_MODES)}")


# ------------------------------------------------------------
# group_rank
# ------------------------------------------------------------
def apply_group_rank(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    by = str(step.get("by") or "")
    column = str(step.get("column") or "")
    for col, label in ((by, "by"), (column, "column")):
        if col not in df.columns:
            raise RuleValidationError([f"group_rank.{label} 列不存在：{col}"])
    topn = int(step.get("topn") or 3)
    mode = str(step.get("mode") or "top")
    asc = mode == "bottom"
    values = pd.to_numeric(df[column], errors="coerce")
    frame = df.assign(_grp_value=values)
    ranked = (
        frame.sort_values("_grp_value", ascending=asc, kind="mergesort")
        .groupby(by, dropna=False, sort=False)
        .head(topn)
        .drop(columns=["_grp_value"])
    )
    return ranked.copy(), f"组内 TopN：按 {by} 分组取 {column} {mode} {topn} → {len(ranked)} 行"


def _validate_group_rank(step, known, tag, errors) -> None:
    for key in ("by", "column"):
        val = step.get(key)
        if not isinstance(val, str) or not val:
            errors.append(f"{tag}.{key} 必须是列名")
        elif known and val not in known:
            errors.append(f"{tag}.{key} 引用了不存在的列 {val!r}")
    topn = step.get("topn")
    if topn is not None and (not isinstance(topn, int) or isinstance(topn, bool) or topn < 1):
        errors.append(f"{tag}.topn 必须是正整数")
    if step.get("mode") is not None and step["mode"] not in AGG_TOPN_MODES:
        errors.append(f"{tag}.mode 非法，可选 {list(AGG_TOPN_MODES)}")


def _group_rank_refs(step: Dict[str, Any]) -> List[str]:
    return [step.get("by"), step.get("column")]


# ------------------------------------------------------------
# anti_join / semi_join
# ------------------------------------------------------------
def _set_join(
    df: pd.DataFrame, step: Dict[str, Any], frames: Dict[str, pd.DataFrame], kind: str
) -> Tuple[pd.DataFrame, str]:
    alias = str(step.get("right") or "")
    right = (frames or {}).get(alias)
    if right is None:
        raise RuleValidationError([f"集合关联数据源不存在：{alias}"])
    left_on = as_cols(step.get("left_on"))
    right_on = as_cols(step.get("right_on")) or left_on
    if not left_on or len(left_on) != len(right_on):
        raise RuleValidationError(["anti_join/semi_join 的 left_on 与 right_on 数量必须一致且非空"])
    for col in left_on:
        if col not in df.columns:
            raise RuleValidationError([f"集合关联左表列不存在：{col}"])
    for col in right_on:
        if col not in right.columns:
            raise RuleValidationError([f"集合关联右表列不存在：{col}"])
    # 右表按键去重，避免一对多造成行数膨胀
    keys = right[right_on].drop_duplicates()
    marker = "__matched__"
    merged = df.merge(
        keys.assign(**{marker: True}), how="left",
        left_on=left_on, right_on=right_on, suffixes=("", "_右表"),
    )
    matched = merged[marker].fillna(False).astype(bool)
    out = merged.loc[matched if kind == "semi" else ~matched].copy()
    out = out[[c for c in df.columns if c in out.columns]]
    label = "半连接（仅保留匹配行）" if kind == "semi" else "反连接（仅保留未匹配行）"
    return out.reset_index(drop=True), f"{label} {alias} → {len(out)}/{len(df)} 行"


def _validate_set_join(step, known, tag, errors) -> None:
    if not isinstance(step.get("right"), str) or not step.get("right"):
        errors.append(f"{tag}.right 必须是数据源别名")
    left_on = as_cols(step.get("left_on"))
    if not left_on:
        errors.append(f"{tag}.left_on 不能为空")
    for col in left_on:
        if known and col not in known:
            errors.append(f"{tag}.left_on 引用了不存在的列 {col!r}")


def _set_join_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("left_on"))


register_op(OpSpec(
    name="corr", layer=2, role="analysis", execute=apply_corr,
    validate=_validate_corr, refs=_corr_refs, outputs=_corr_outputs,
    params=(ParamSpec("columns", list, column=True, multi=True),
            ParamSpec("method", str, enum=CORR_METHODS)),
    summary="数值列相关性矩阵（自动排除 ID / 常量列）", task_hint="correlation",
    prompt_example={"op": "corr", "columns": ["<数值列1>", "<数值列2>"], "method": "pearson"},
))
register_op(OpSpec(
    name="having", layer=1, role="filter", execute=apply_having,
    validate=_validate_having, refs=lambda s: [s.get("column")],
    params=(ParamSpec("column", str, required=True, column=True),),
    summary="聚合结果再筛选（阈值比较或 TopN）", task_hint="ranking",
    prompt_example={"op": "having", "column": "条数", "operator": "gte", "value": 10},
))
register_op(OpSpec(
    name="group_rank", layer=2, role="analysis", execute=apply_group_rank,
    validate=_validate_group_rank, refs=_group_rank_refs,
    params=(ParamSpec("by", str, required=True, column=True),
            ParamSpec("column", str, required=True, column=True)),
    summary="组内 TopN / BottomN", task_hint="ranking",
    prompt_example={"op": "group_rank", "by": "<分组列>", "column": "<数值列>", "topn": 3},
))
for _name, _kind, _summary in (
    ("anti_join", "anti", "反连接：仅保留右表中不存在匹配键的行（差集/对账）"),
    ("semi_join", "semi", "半连接：仅保留右表中存在匹配键的行（不引入右表列）"),
):
    register_op(OpSpec(
        name=_name, layer=2, role="analysis",
        execute=lambda df, step, frames=None, ctx=None, k=_kind: _set_join(df, step, frames or {}, k),
        validate=_validate_set_join, refs=_set_join_refs,
        params=(ParamSpec("right", str, required=True),),
        summary=_summary, task_hint="join",
        prompt_example={"op": _name, "right": "<别名>", "left_on": "<主表键>", "right_on": "<右表键>"},
    ))

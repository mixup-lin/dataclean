# -*- coding: utf-8 -*-
"""聚合算子：aggregate。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import AGG_WHITELIST, aggregate_pairs, as_list
from ops.errors import RuleValidationError
from ops.primitives import aggregate_func, existing_cols
from ops.registry import OpSpec, ParamSpec, register_op


def apply_aggregate(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    bys = existing_cols(df, step.get("by"))
    if not bys:
        raise RuleValidationError(["aggregate 分组列不存在"])
    pairs = aggregate_pairs(step)
    if not pairs:
        raise RuleValidationError(["aggregate 缺少聚合列"])
    specs: Dict[str, Any] = {}
    for col, func, alias in pairs:
        if col not in df.columns:
            raise RuleValidationError([f"aggregate 聚合列不存在：{col}"])
        specs[alias] = aggregate_func(col, func, step)
    grouped = df.groupby(bys, dropna=False).agg(**specs).reset_index()
    desc = ", ".join(f"{col}({func})→{alias}" for col, func, alias in pairs)
    return grouped, f"按 {','.join(bys)} 聚合 {desc}"


def _refs(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("by")) + as_list(step.get("column"))


def _outputs(step: Dict[str, Any]) -> List[str]:
    return [alias for _c, _f, alias in aggregate_pairs(step)] + [str(b) for b in as_list(step.get("by"))]


def _validate(step, known, tag, errors) -> None:
    bys = as_list(step.get("by"))
    if not bys or not all(isinstance(x, str) and x for x in bys):
        errors.append(f"{tag}.by 必须是字符串或字符串数组")
    for col in bys:
        if isinstance(col, str) and known and col not in known:
            errors.append(f"{tag}.by 引用了不存在的列 {col!r}")
    cols = as_list(step.get("column"))
    funcs = as_list(step.get("func")) or ["count"]
    if not cols:
        errors.append(f"{tag}.column 不能为空")
    for col in cols:
        if not isinstance(col, str) or not col:
            errors.append(f"{tag}.column 必须是字符串或字符串数组")
        elif known and col not in known:
            errors.append(f"{tag}.column 引用了不存在的列 {col!r}")
    for func in funcs:
        if func not in AGG_WHITELIST:
            errors.append(f"{tag}.func 非法聚合函数 {func!r}")
    if len(funcs) not in (1, len(cols)) and len(cols) != 1:
        errors.append(f"{tag}.func 数量需为 1 或与 column 数量一致")
    alias = step.get("alias")
    if alias is not None and not isinstance(alias, (str, list)):
        errors.append(f"{tag}.alias 必须是字符串或字符串数组")


register_op(OpSpec(
    name="aggregate", layer=2, role="analysis", execute=apply_aggregate, validate=_validate,
    params=(ParamSpec("by", (str, list), required=True),
            ParamSpec("column", None), ParamSpec("func", None), ParamSpec("alias", None)),
    refs=_refs, outputs=_outputs, summary="分组聚合", task_hint="summary",
    prompt_example={"op": "aggregate", "by": ["<分组列>"], "column": ["<数值列>"], "func": ["sum", "count"], "alias": ["<结果列>"]},
))


# ------------------------------------------------------------
# count_rows：分组计数（P0）
# 「统计每个小区有几条数据」不再被迫借某个值列做 count；
# 直接 groupby.size()，缺失值计价列也不会导致少算。
# ------------------------------------------------------------
def apply_count_rows(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    bys = existing_cols(df, step.get("by"))
    if not bys:
        raise RuleValidationError(["count_rows 分组列不存在"])
    alias = str(step.get("alias") or "条数").strip() or "条数"
    grouped = df.groupby(bys, dropna=False).size().reset_index(name=alias)
    if step.get("sort", True):
        grouped = grouped.sort_values(alias, ascending=False, kind="mergesort")
    return grouped, f"按 {','.join(bys)} 统计行数 → {alias}（{len(grouped)} 组）"


def _count_rows_outputs(step: Dict[str, Any]) -> List[str]:
    return [str(step.get("alias") or "条数")] + [str(b) for b in as_list(step.get("by"))]


def _count_rows_refs(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("by"))


def _validate_count_rows(step, known, tag, errors) -> None:
    bys = as_list(step.get("by"))
    if not bys or not all(isinstance(x, str) and x for x in bys):
        errors.append(f"{tag}.by 必须是字符串或字符串数组")
    for col in bys:
        if isinstance(col, str) and known and col not in known:
            errors.append(f"{tag}.by 引用了不存在的列 {col!r}")
    alias = step.get("alias")
    if alias is not None and not isinstance(alias, str):
        errors.append(f"{tag}.alias 必须是字符串")
    if step.get("sort") is not None and not isinstance(step["sort"], bool):
        errors.append(f"{tag}.sort 必须是布尔值")


register_op(OpSpec(
    name="count_rows", layer=2, role="analysis", execute=apply_count_rows,
    validate=_validate_count_rows, refs=_count_rows_refs, outputs=_count_rows_outputs,
    params=(ParamSpec("by", (str, list), required=True),
            ParamSpec("alias", str), ParamSpec("sort", bool)),
    summary="分组统计每个组的数据条数（无需指定计数值列）", task_hint="summary",
    prompt_example={"op": "count_rows", "by": ["<分组列>"], "alias": "条数"},
))

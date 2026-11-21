# -*- coding: utf-8 -*-
"""占比算子：pct（条件 / 构成 / 组内），固定产出 分子 / 分母 / 占比。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from formatting import _fmt_num
from ops.conditions import build_mask, check_condition_or_compound, describe_condition
from ops.constants import (
    PCT_DEN_COL, PCT_NUM_COL, PCT_SCOPES, as_list, pct_alias,
)
from ops.errors import RuleValidationError
from ops.primitives import existing_cols, pct_values
from ops.registry import OpSpec, ParamSpec, register_op


def apply_pct(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    scope = str(step.get("scope"))
    alias = pct_alias(step)

    if scope == "condition":
        cond = step.get("condition")
        if not isinstance(cond, dict):
            raise RuleValidationError(["条件占比需要 condition"])
        mask = build_mask(df, cond)
        numerator = int(mask.sum())
        denominator = int(len(df))
        ratio = round(numerator / denominator * 100, 4) if denominator else 0.0
        frame = pd.DataFrame([
            {"条件": describe_condition(cond), PCT_NUM_COL: numerator, PCT_DEN_COL: denominator, alias: ratio}
        ])
        return frame, f"条件占比 {numerator}/{denominator} = {ratio}%"

    bys = existing_cols(df, step.get("by"))
    if not bys:
        raise RuleValidationError(["占比需要存在的 by 分组列"])
    values = pct_values(df, step.get("column"))
    frame = df.copy()

    if scope == "overall":
        total = float(values.sum())
        frame[PCT_NUM_COL] = values
        frame[PCT_DEN_COL] = total
        frame[alias] = (values / total * 100).round(4) if total else 0.0
        note = f"构成占比：分母 {_fmt_num(total)}"
        if step.get("cumulative"):
            frame = frame.sort_values(alias, ascending=False, kind="mergesort")
            frame[alias] = frame[alias].cumsum().round(4)
            note += "，已按占比降序累计（帕累托）"
        return frame, note

    if scope != "group":
        raise RuleValidationError([f"未知占比口径：{scope}"])
    within = [c for c in as_list(step.get("within")) if c in frame.columns]
    if not within:
        raise RuleValidationError(["组内占比需要存在的 within 列"])
    denominator = values.groupby([frame[c] for c in bys], dropna=False).transform("sum")
    keys = bys + [c for c in within if c not in bys]
    den = pd.Series(denominator, index=frame.index).replace(0, np.nan)
    frame[PCT_NUM_COL] = values
    frame[PCT_DEN_COL] = den.fillna(0)
    frame[alias] = (values / den * 100).round(4).fillna(0.0)
    frame = frame.sort_values(keys + [alias], ascending=[True] * len(keys) + [False],
                              kind="mergesort", na_position="last")
    return frame, f"组内占比：按 {'/'.join(bys)} 分组、{'/'.join(within)} 组内归一"


def _condition_columns(step: Dict[str, Any]) -> List[str]:
    cond = step.get("condition")
    if not isinstance(cond, dict):
        return []
    if isinstance(cond.get("conditions"), list):
        return [c.get("column") for c in cond["conditions"] if isinstance(c, dict) and c.get("column")]
    return [cond.get("column")] if cond.get("column") else []


def _refs(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("by")) + as_list(step.get("column")) + as_list(step.get("within")) + _condition_columns(step)


def _outputs(step: Dict[str, Any]) -> List[str]:
    return [PCT_NUM_COL, PCT_DEN_COL, pct_alias(step)]


def _validate(step, known, tag, errors) -> None:
    scope = step.get("scope")
    if scope not in PCT_SCOPES:
        errors.append(f"{tag}.scope 非法，可选 {list(PCT_SCOPES)}")
        return
    for key in ("by", "column", "within"):
        for col in as_list(step.get(key)):
            if not isinstance(col, str) or not col:
                errors.append(f"{tag}.{key} 必须是字符串或字符串数组")
            elif known and col not in known:
                errors.append(f"{tag}.{key} 引用了不存在的列 {col!r}")
    if scope == "condition":
        condition = step.get("condition")
        if not isinstance(condition, dict):
            errors.append(f"{tag}.condition 必须是对象")
        else:
            check_condition_or_compound(condition, f"{tag}.condition", known, errors)
    if scope == "group" and not step.get("within"):
        errors.append(f"{tag}.within 在组内占比时必须提供")
    if scope == "overall" and not (step.get("by") or step.get("column")):
        errors.append(f"{tag} 需提供 by 或 column")
    if step.get("cumulative") is not None and not isinstance(step["cumulative"], bool):
        errors.append(f"{tag}.cumulative 必须是布尔值")


register_op(OpSpec(
    name="pct", layer=2, role="analysis", execute=apply_pct, validate=_validate,
    params=(ParamSpec("scope", str, required=True, enum=PCT_SCOPES),),
    refs=_refs, outputs=_outputs, preserve_refs=_condition_columns,
    summary="占比：条件占比 / 构成占比 / 组内占比", task_hint="proportion",
    prompt_example={"op": "pct", "scope": "condition", "condition": {"column": "<列>", "operator": "gt", "value": 0}, "alias": "<占比列>"},
))

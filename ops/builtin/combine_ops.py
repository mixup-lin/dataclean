# -*- coding: utf-8 -*-
"""多表算子：join / union（包装 sources 的合并能力，含膨胀保护）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ops.constants import JOIN_HOWS, as_list
from ops.errors import RuleValidationError
from ops.registry import OpSpec, ParamSpec, register_op
from ops.primitives import as_cols
from sources import join_frames, union_frames


def apply_join(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    alias = str(step.get("right"))
    source_frame = (frames or {}).get(alias)
    if source_frame is None:
        raise RuleValidationError([f"关联数据源不存在：{alias}"])
    merged, stats = join_frames(
        df, source_frame,
        how=str(step.get("how") or "left"),
        left_on=as_cols(step.get("left_on")),
        right_on=as_cols(step.get("right_on")) or as_cols(step.get("left_on")),
        suffixes=tuple(step.get("suffixes") or ("", "_右表")),
    )
    if ctx is not None:
        ctx.setdefault("joins", []).append({"alias": alias, **stats})
    return merged, (
        f"{stats['how']} join {alias}：匹配 {stats['matched']} 行"
        f"（{stats['match_rate']}%），未匹配 {stats['unmatched_left']} 行"
    )


def apply_union(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    alias = str(step.get("right"))
    source_frame = (frames or {}).get(alias)
    if source_frame is None:
        raise RuleValidationError([f"合并数据源不存在：{alias}"])
    merged, stats = union_frames(df, source_frame)
    if ctx is not None:
        ctx.setdefault("unions", []).append({"alias": alias, **stats})
    return merged, f"纵向合并 {alias}：{stats['rows']} 行（{len(stats['columns'])} 列）"


def _refs_join(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("left_on"))


def _validate_join(step, known, tag, errors) -> None:
    if not isinstance(step.get("right"), str) or not step.get("right"):
        errors.append(f"{tag}.right 必须是数据源别名")
    if step.get("how") not in JOIN_HOWS:
        errors.append(f"{tag}.how 非法，可选 {list(JOIN_HOWS)}")
    if not step.get("left_on"):
        errors.append(f"{tag}.left_on 不能为空")
    for col in as_list(step.get("left_on")):
        if not isinstance(col, str) or not col:
            errors.append(f"{tag}.left_on 必须是字符串或字符串数组")
        elif known and col not in known:
            errors.append(f"{tag}.left_on 引用了不存在的列 {col!r}")


def _validate_union(step, known, tag, errors) -> None:
    if not isinstance(step.get("right"), str) or not step.get("right"):
        errors.append(f"{tag}.right 必须是数据源别名")


register_op(OpSpec(
    name="join", layer=2, role="analysis", execute=apply_join, validate=_validate_join,
    params=(ParamSpec("right", str, required=True), ParamSpec("how", str, enum=JOIN_HOWS)),
    refs=_refs_join, summary="多表关联（left/inner/right/outer）", task_hint="join",
    prompt_example={"op": "join", "right": "<别名>", "how": "left", "left_on": "<主表键>", "right_on": "<右表键>"},
))
register_op(OpSpec(
    name="union", layer=2, role="analysis", execute=apply_union, validate=_validate_union,
    params=(ParamSpec("right", str, required=True),),
    summary="纵向合并多表", task_hint="join",
    prompt_example={"op": "union", "right": "<别名>"},
))

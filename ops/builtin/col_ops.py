# -*- coding: utf-8 -*-
"""列操作：select / drop_columns / rename。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import as_list
from ops.errors import RuleValidationError
from ops.primitives import existing_cols
from ops.registry import OpSpec, ParamSpec, register_op


def apply_select(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    cols = existing_cols(df, step.get("columns"))
    if not cols:
        raise RuleValidationError(["select 未匹配到任何有效列"])
    return df[cols].copy(), f"保留 {len(cols)} 列"


def apply_drop_columns(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    cols = existing_cols(df, step.get("columns"))
    return df.drop(columns=cols).copy(), f"删除 {len(cols)} 列"


def apply_rename(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    mapping = {k: v for k, v in (step.get("mapping") or {}).items() if k in df.columns}
    return df.rename(columns=mapping).copy(), f"重命名 {len(mapping)} 列"


def _refs_columns(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("columns"))


register_op(OpSpec(
    name="select", layer=1, role="transform", execute=apply_select,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),),
    refs=_refs_columns, summary="仅保留指定列", task_hint="",
))
register_op(OpSpec(
    name="drop_columns", layer=1, role="transform", execute=apply_drop_columns,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),),
    refs=_refs_columns, summary="删除指定列",
))
register_op(OpSpec(
    name="rename", layer=1, role="transform", execute=apply_rename,
    params=(ParamSpec("mapping", dict, required=True),),
    summary="按 {旧列: 新列} 重命名",
))

# -*- coding: utf-8 -*-
"""重塑算子：pivot（交叉表）。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import PIVOT_AGG_WHITELIST, as_list
from ops.errors import RuleValidationError
from ops.primitives import MAX_PIVOT_COLUMNS, existing_cols
from ops.registry import OpSpec, ParamSpec, register_op


def apply_pivot(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    index = existing_cols(df, step.get("index"))
    dims = existing_cols(df, step.get("columns"))
    values = step.get("values")
    aggfunc = str(step.get("aggfunc") or "mean")
    if not index or not dims:
        raise RuleValidationError(["pivot 行维度或列维度不存在"])
    if not isinstance(values, str) or values not in df.columns:
        raise RuleValidationError(["pivot 数值列不存在"])
    table = pd.pivot_table(df, index=index, columns=dims, values=values, aggfunc=aggfunc, dropna=False)
    if isinstance(table.columns, pd.MultiIndex):
        table.columns = ["_".join(str(x) for x in col if str(x) != "") for col in table.columns]
    table.columns = [str(c) for c in table.columns]
    if len(table.columns) > MAX_PIVOT_COLUMNS:
        order = table.sum(numeric_only=True).sort_values(ascending=False)
        keep = list(order.index[: MAX_PIVOT_COLUMNS - len(index)])
        table = table[keep]
    table = table.reset_index()
    return table, f"透视表 {len(table)} 行 × {len(table.columns)} 列（{aggfunc}）"


def apply_melt(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    """宽表转长表：保留 id_vars，其余度量列压成「变量 / 值」两列。"""
    id_vars = existing_cols(df, step.get("id_vars"))
    value_vars = existing_cols(df, step.get("value_vars"))
    if not value_vars:
        value_vars = [str(c) for c in df.columns if c not in id_vars]
    if not value_vars:
        raise RuleValidationError(["melt 未匹配到任何度量列"])
    var_name = str(step.get("var_name") or "变量")
    value_name = str(step.get("value_name") or "值")
    table = df.melt(
        id_vars=id_vars, value_vars=value_vars,
        var_name=var_name, value_name=value_name,
    )
    return table, (
        f"宽转长：{len(df)} 行 × {len(value_vars)} 列 → {len(table)} 行"
        f"（{var_name} / {value_name}）"
    )


def _melt_outputs(step: Dict[str, Any]) -> List[str]:
    return [
        str(step.get("var_name") or "变量"),
        str(step.get("value_name") or "值"),
    ]


def _melt_refs(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("id_vars")) + as_list(step.get("value_vars"))


def _validate_melt(step, known, tag, errors) -> None:
    for key in ("id_vars", "value_vars"):
        value = step.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            errors.append(f"{tag}.{key} 必须是列名数组")
    if not step.get("value_vars") and not step.get("id_vars"):
        errors.append(f"{tag} 需提供 id_vars 或 value_vars")


def _refs(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("index")) + as_list(step.get("columns")) + as_list(step.get("values"))


def _outputs(step: Dict[str, Any]) -> List[str]:
    return [str(c) for c in as_list(step.get("index"))]


def _validate(step, known, tag, errors) -> None:
    for key in ("index", "columns", "values"):
        for col in as_list(step.get(key)):
            if not isinstance(col, str) or not col:
                errors.append(f"{tag}.{key} 必须是字符串或字符串数组")
            elif known and col not in known:
                errors.append(f"{tag}.{key} 引用了不存在的列 {col!r}")
    aggfunc = step.get("aggfunc")
    if aggfunc is not None and aggfunc not in PIVOT_AGG_WHITELIST:
        errors.append(f"{tag}.aggfunc 非法，可选 {list(PIVOT_AGG_WHITELIST)}")


register_op(OpSpec(
    name="melt", layer=2, role="analysis", execute=apply_melt, validate=_validate_melt,
    params=(ParamSpec("id_vars", list, column=True, multi=True),
            ParamSpec("value_vars", list, column=True, multi=True)),
    refs=_melt_refs, outputs=_melt_outputs,
    summary="宽表转长表（id_vars 保留，度量列压成变量/值）",
    prompt_example={"op": "melt", "id_vars": ["<维度列>"], "value_vars": ["<度量列1>", "<度量列2>"], "var_name": "变量", "value_name": "值"},
))
register_op(OpSpec(
    name="pivot", layer=2, role="analysis", execute=apply_pivot, validate=_validate,
    params=(ParamSpec("index", (str, list), required=True),
            ParamSpec("columns", str, required=True),
            ParamSpec("values", str, required=True)),
    refs=_refs, outputs=_outputs, summary="交叉表透视", task_hint="pivot",
    prompt_example={"op": "pivot", "index": "<行维度>", "columns": "<列维度>", "values": "<数值列>", "aggfunc": "sum"},
))

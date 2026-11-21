# -*- coding: utf-8 -*-
"""二分类比率算子 ``rate``：把「某列为空/非空」或任意条件分成两类，
一次给出两类条数、总数、目标率与两类比值。

与 ``pct`` 的区别
----------------
``pct(scope=condition)`` 只回答「满足条件占全部的比例」，不给出另一类数量与
两类比值；``assign + count_rows`` 又会在清洗填充缺失后把两类合并成一类。
``rate`` 是「A 为空=X，不为空=Y，统计 X率 / Y率 / X与Y之比」这类需求的
**统一最终算子**，方向由 ``numerator`` 决定、分母由 ``denominator`` 决定，
因此换一种问法（成交率 → 未成交率 → 分组成交率）不需要新增算子，也不需要
改写执行链路。

契约
----
- ``condition``：定义「命中类」的布尔条件，省略时默认 ``column`` 非空；
- ``true_label`` / ``false_label``：命中类 / 未命中类的业务标签（成交 / 未成交）；
- ``numerator``：``hit``（默认，命中类）或 ``miss``（未命中类）；
- ``denominator``：``total``（默认，两类总数）/ ``hit`` / ``miss``；
  后两者用于「A/B 之比」这类口径，此时率列是比值 × 100；
- ``alias``：目标率列名，缺省按 numerator/denominator 自动推导；
- ``by``：分组维度，产出一行一个分组的率。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from ops.conditions import (
    build_mask, check_condition_or_compound, describe_condition,
)
from ops.constants import as_list
from ops.errors import RuleValidationError
from ops.registry import OpSpec, ParamSpec, register_op

DEFAULT_TRUE_LABEL = "命中"
DEFAULT_FALSE_LABEL = "未命中"
NUMERATORS = ("hit", "miss")
DENOMINATORS = ("total", "hit", "miss")


def labels(step: Dict[str, Any]) -> Tuple[str, str]:
    """两类标签，可由规则覆盖（如 成交 / 未成交）。"""
    true_label = step.get("true_label")
    false_label = step.get("false_label")
    t = str(true_label).strip() if isinstance(true_label, str) and true_label.strip() else DEFAULT_TRUE_LABEL
    f = str(false_label).strip() if isinstance(false_label, str) and false_label.strip() else DEFAULT_FALSE_LABEL
    return t, f


def numerator_of(step: Dict[str, Any]) -> str:
    raw = str(step.get("numerator") or "hit").strip().lower()
    return raw if raw in NUMERATORS else "hit"


def denominator_of(step: Dict[str, Any]) -> str:
    raw = str(step.get("denominator") or "total").strip().lower()
    return raw if raw in DENOMINATORS else "total"


def rate_alias(step: Dict[str, Any]) -> str:
    """目标率列名：显式 alias 优先，否则按分子/分母口径自动推导。"""
    alias = step.get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    t, f = labels(step)
    num, den = numerator_of(step), denominator_of(step)
    source = t if num == "hit" else f
    if den == "total":
        return f"{source}率"
    if den == "hit":
        return f"{source}/{t}" if num != "hit" else f"{t}率"
    return f"{source}/{f}" if num != "miss" else f"{f}率"


def rate_columns(step: Dict[str, Any]) -> List[str]:
    """结果列（保序去重），供契约校验与报告识别。"""
    t, f = labels(step)
    cols = [f"{t}数", f"{f}数", "总数", rate_alias(step), f"{t}{f}比"]
    seen: set = set()
    return [c for c in cols if not (c in seen or seen.add(c))]


def _condition(step: Dict[str, Any]) -> Dict[str, Any]:
    """未显式给条件时，默认按 ``column`` 非空作为「命中」。"""
    cond = step.get("condition")
    if isinstance(cond, dict) and cond:
        return cond
    return {"column": str(step.get("column") or ""), "operator": "notna"}


def _condition_columns(step: Dict[str, Any]) -> List[str]:
    cond = _condition(step)
    if isinstance(cond.get("conditions"), list):
        return [
            str(c.get("column")) for c in cond["conditions"]
            if isinstance(c, dict) and isinstance(c.get("column"), str) and c.get("column")
        ]
    col = cond.get("column")
    return [str(col)] if isinstance(col, str) and col else []


def _denominator_label(step: Dict[str, Any]) -> str:
    den = denominator_of(step)
    if den == "total":
        return "总数"
    t, f = labels(step)
    return f"{t}数" if den == "hit" else f"{f}数"


def apply_rate(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    """按条件把数据分成两类，输出两类条数、总数、目标率与两类比值。"""
    column = str(step.get("column") or "")
    if column not in df.columns:
        raise RuleValidationError([f"rate 判空列不存在：{column!r}"])
    cond = _condition(step)
    mask = build_mask(df, cond).fillna(False).astype(bool)
    bys = [c for c in as_list(step.get("by")) if c in df.columns]
    t, f = labels(step)
    alias = rate_alias(step)
    true_col, false_col = f"{t}数", f"{f}数"
    ratio_col = f"{t}{f}比"
    num_col = true_col if numerator_of(step) == "hit" else false_col
    den_col = _denominator_label(step)

    work = pd.DataFrame(index=df.index)
    work["_hit"] = mask.astype(int)
    work["_n"] = 1
    if bys:
        for c in bys:
            work[c] = df[c].to_numpy()
        grouped = work.groupby(bys, dropna=False)[["_hit", "_n"]].sum().reset_index()
        out = grouped.rename(columns={"_hit": true_col, "_n": "总数"})
        out[false_col] = out["总数"] - out[true_col]
        ordered = bys + [true_col, false_col, "总数"]
    else:
        hit = int(mask.sum())
        out = pd.DataFrame([{true_col: hit, false_col: int(len(df)) - hit,
                             "总数": int(len(df))}])
        ordered = [true_col, false_col, "总数"]
    numerator = pd.to_numeric(out[num_col], errors="coerce").fillna(0)
    denominator = pd.to_numeric(out[den_col], errors="coerce").replace(0, np.nan)
    out[alias] = (numerator / denominator * 100).round(4).fillna(0.0)
    if ratio_col != alias:
        out[ratio_col] = (
            pd.to_numeric(out[true_col], errors="coerce")
            / pd.to_numeric(out[false_col], errors="coerce").replace(0, np.nan)
        ).round(4).fillna(0.0)
    columns = [c for c in ordered + [alias, ratio_col]
               if c in out.columns and c not in ("_hit", "_n")]
    seen: set = set()
    columns = [c for c in columns if not (c in seen or seen.add(c))]
    out = out[columns]
    rate_value = out[alias].iloc[0] if len(out) else 0.0
    note = (
        f"二分类比率（{describe_condition(cond)}）：{t} {int(out[true_col].iloc[0])} / "
        f"{f} {int(out[false_col].iloc[0])}，"
        f"{alias} = {num_col}/{den_col} = {rate_value}%"
    )
    return out, note


def _refs(step: Dict[str, Any]) -> List[str]:
    return [str(step.get("column") or "")] + as_list(step.get("by")) + _condition_columns(step)


def _outputs(step: Dict[str, Any]) -> List[str]:
    return rate_columns(step)


def _preserve_refs(step: Dict[str, Any]) -> List[str]:
    """判空列必须在清洗后还原原始空值，否则填充会抹掉两类区分。"""
    col = step.get("column")
    out = [str(col)] if isinstance(col, str) and col else []
    out += _condition_columns(step)
    return list(dict.fromkeys(out))


def _validate(step, known, tag, errors) -> None:
    column = step.get("column")
    if not isinstance(column, str) or not column:
        errors.append(f"{tag}.column 必须是指定的判空列")
    elif known and column not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {column!r}")
    for key in ("by",):
        for col in as_list(step.get(key)):
            if not isinstance(col, str) or not col:
                errors.append(f"{tag}.{key} 必须是字符串或字符串数组")
            elif known and col not in known:
                errors.append(f"{tag}.{key} 引用了不存在的列 {col!r}")
    cond = step.get("condition")
    if cond is not None:
        if not isinstance(cond, dict) or not cond:
            errors.append(f"{tag}.condition 必须是非空对象")
        else:
            check_condition_or_compound(cond, f"{tag}.condition", known, errors)
    for key in ("true_label", "false_label", "alias", "numerator", "denominator"):
        if key in step and not isinstance(step[key], str):
            errors.append(f"{tag}.{key} 必须是字符串")
    num = step.get("numerator")
    if num is not None and isinstance(num, str) and num.strip().lower() not in NUMERATORS:
        errors.append(f"{tag}.numerator 必须是 {list(NUMERATORS)}")
    den = step.get("denominator")
    if den is not None and isinstance(den, str) and den.strip().lower() not in DENOMINATORS:
        errors.append(f"{tag}.denominator 必须是 {list(DENOMINATORS)}")
    t, f = labels(step)
    if t == f:
        errors.append(f"{tag}.true_label 与 false_label 不能相同")
    alias = rate_alias(step)
    if alias in (f"{t}数", f"{f}数", "总数", f"{t}{f}比"):
        errors.append(f"{tag}.alias 不能与固有结果列同名：{alias!r}")


register_op(OpSpec(
    name="rate", layer=2, role="analysis", execute=apply_rate,
    validate=_validate, refs=_refs, outputs=_outputs,
    preserve_refs=_preserve_refs,
    params=(
        ParamSpec("column", str, required=True, column=True),
        ParamSpec("by", None, multi=True),
        ParamSpec("condition", dict),
        ParamSpec("true_label", str),
        ParamSpec("false_label", str),
        ParamSpec("alias", str),
        ParamSpec("numerator", str, enum=NUMERATORS),
        ParamSpec("denominator", str, enum=DENOMINATORS),
    ),
    summary="二分类比率：按条件/判空分两类，方向可调（命中或未命中），输出两类条数、总数、目标率与两类比值",
    task_hint="proportion",
    prompt_example={
        "op": "rate", "column": "<判空列>",
        "condition": {"column": "<判空列>", "operator": "notna"},
        "true_label": "成交", "false_label": "未成交",
        "numerator": "miss", "alias": "未成交率",
    },
))

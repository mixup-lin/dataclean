# -*- coding: utf-8 -*-
"""行操作：filter / sort / head / sample / dedupe。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ops.conditions import check_filter, describe_condition, build_mask, single_condition
from ops.constants import DEDUPE_KEEP, DROPNA_HOWS, as_list
from ops.errors import RuleValidationError
from ops.primitives import as_cols, existing_cols
from ops.registry import OpSpec, ParamSpec, register_op


def _filter_columns(step: Dict[str, Any]) -> List[str]:
    conditions = step.get("conditions")
    if isinstance(conditions, list):
        return [c.get("column") for c in conditions if isinstance(c, dict) and c.get("column")]
    return [step.get("column")] if step.get("column") else []


def apply_filter(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    conditions = step.get("conditions")
    if isinstance(conditions, list) and conditions:
        logic = str(step.get("logic", "and")).lower()
        mask: Optional[pd.Series] = None
        for cond in conditions:
            part = build_mask(df, cond)
            if mask is None:
                mask = part
            else:
                mask = (mask & part) if logic != "or" else (mask | part)
        if mask is None:
            mask = pd.Series(True, index=df.index)
        kept = int(mask.sum())
        return df[mask].copy(), f"{len(conditions)} 个条件({logic}) → 保留 {kept}/{len(df)} 行"
    cond = single_condition(step)
    mask = build_mask(df, cond)
    return df[mask].copy(), f"{describe_condition(cond)} → 保留 {int(mask.sum())} 行"


def apply_sort(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    bys = existing_cols(df, step.get("by"))
    if not bys:
        raise RuleValidationError(["sort 未匹配到任何有效列"])
    ascending = step.get("ascending", True)
    if isinstance(ascending, list):
        flags = [bool(v) for v in ascending][: len(bys)]
        if len(flags) < len(bys):
            flags += [flags[-1] if flags else True] * (len(bys) - len(flags))
    else:
        flags = bool(ascending)
    return (
        df.sort_values(
            by=bys, ascending=flags, kind="mergesort",
            na_position=str(step.get("na_position", "last")),
        ).copy(),
        f"按 {','.join(bys)} 排序",
    )


def apply_head(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    n = int(step["n"])
    return df.head(n).copy(), f"取前 {n} 行"


def apply_sample(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    n = step.get("n")
    frac = step.get("frac")
    seed = step.get("random_state", 42)
    if isinstance(n, int) and n > 0:
        out = df.sample(n=min(n, len(df)), random_state=seed)
    elif frac:
        out = df.sample(frac=float(frac), random_state=seed)
    else:
        out = df.sample(frac=0.1, random_state=seed)
    return out.copy(), f"抽样 {len(out)}/{len(df)} 行"


def apply_dedupe(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    subset = as_cols(step.get("subset")) or None
    keep = str(step.get("keep") or "first")
    before = len(df)
    out = df.drop_duplicates(subset=subset, keep=keep)
    return out, f"去重 {before - len(out)} 行"


def apply_tail(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    n = int(step["n"])
    return df.tail(n).copy(), f"取后 {n} 行"


def apply_dropna(df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None) -> Tuple[pd.DataFrame, str]:
    subset = as_cols(step.get("subset")) or None
    how = str(step.get("how") or "any")
    thresh = step.get("thresh")
    before = len(df)
    # pandas 不允许同时传 how 与 thresh：thresh 优先（它本身就是"至少保留几个非空"）
    if isinstance(thresh, int):
        out = df.dropna(subset=subset, thresh=int(thresh))
        rule_desc = f"至少 {thresh} 个非空"
    else:
        out = df.dropna(subset=subset, how=how)
        rule_desc = how
    scope = ",".join(subset) if subset else "全部列"
    return out, f"删除缺失行 {before - len(out)}（{scope}·{rule_desc}）"


def _validate_sort(step, known, tag, errors) -> None:
    if step.get("ascending") is not None and not isinstance(step.get("ascending"), (bool, list)):
        errors.append(f"{tag}.ascending 必须是布尔值或布尔数组")
    if step.get("na_position") not in (None, "first", "last"):
        errors.append(f"{tag}.na_position 必须是 first / last")


def _validate_head(step, known, tag, errors) -> None:
    if isinstance(step.get("n"), int) and step["n"] < 1:
        errors.append(f"{tag}.n 必须为正整数")


def _validate_dedupe(step, known, tag, errors) -> None:
    if step.get("keep") is not None and step["keep"] not in DEDUPE_KEEP:
        errors.append(f"{tag}.keep 非法，可选 {list(DEDUPE_KEEP)}")


def _validate_tail(step, known, tag, errors) -> None:
    _validate_head(step, known, tag, errors)


def _validate_dropna(step, known, tag, errors) -> None:
    if step.get("how") is not None and step["how"] not in DROPNA_HOWS:
        errors.append(f"{tag}.how 非法，可选 {list(DROPNA_HOWS)}")
    thresh = step.get("thresh")
    if thresh is not None and (not isinstance(thresh, int) or isinstance(thresh, bool) or thresh < 1):
        errors.append(f"{tag}.thresh 必须是正整数")


def _refs_dropna(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("subset"))


def _refs_by(step: Dict[str, Any]) -> List[str]:
    return as_list(step.get("by"))


def _validate_filter(step, known, tag, errors) -> None:
    """适配 conditions 模块的 (step, tag, known, errors) 参数顺序。"""
    check_filter(step, tag, known, errors)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_sample(step, known, tag, errors) -> None:
    n, frac = step.get("n"), step.get("frac")
    if n is None and frac is None:
        errors.append(f"{tag} 需提供 n 或 frac")
    if n is not None and (not isinstance(n, int) or n < 1):
        errors.append(f"{tag}.n 必须是正整数")
    if frac is not None and not (_is_number(frac) and 0 < frac <= 1):
        errors.append(f"{tag}.frac 必须在 (0, 1] 区间")


register_op(OpSpec(
    name="filter", layer=1, role="filter", execute=apply_filter, validate=_validate_filter,
    refs=_filter_columns, preserve_refs=_filter_columns,
    summary="按单条件或复合条件筛选行",
    prompt_example={"op": "filter", "column": "<列>", "operator": "gt", "value": 0},
))
register_op(OpSpec(
    name="sort", layer=1, role="filter", execute=apply_sort, validate=_validate_sort, refs=_refs_by,
    params=(ParamSpec("by", (str, list), required=True, column=True),),
    summary="按列排序",
    prompt_example={"op": "sort", "by": "<列>", "ascending": False},
))
register_op(OpSpec(
    name="head", layer=1, role="filter", execute=apply_head, validate=_validate_head,
    params=(ParamSpec("n", int, required=True),),
    summary="取前 N 行",
    prompt_example={"op": "head", "n": 10},
))
register_op(OpSpec(
    name="sample", layer=1, role="filter", execute=apply_sample, validate=_validate_sample,
    summary="随机抽样（n 或 frac）",
    prompt_example={"op": "sample", "n": 1000, "random_state": 42},
))
register_op(OpSpec(
    name="dedupe", layer=1, role="filter", execute=apply_dedupe, validate=_validate_dedupe,
    summary="按 subset 去重",
    prompt_example={"op": "dedupe", "subset": ["<列>"], "keep": "first"},
))
register_op(OpSpec(
    name="tail", layer=1, role="filter", execute=apply_tail, validate=_validate_tail,
    params=(ParamSpec("n", int, required=True),),
    summary="取后 N 行",
    prompt_example={"op": "tail", "n": 10},
))
register_op(OpSpec(
    name="dropna", layer=1, role="filter", execute=apply_dropna, validate=_validate_dropna,
    params=(ParamSpec("subset", list, column=True, multi=True),),
    refs=_refs_dropna, preserve_refs=_refs_dropna,
    summary="按列删除缺失行（subset / how / thresh；不指定 subset 时作用于全部列）",
    prompt_example={"op": "dropna", "subset": ["<列>"], "how": "any"},
    # 未指定 subset 时，执行器需还原全部列的原始空值，否则清洗填充会让本步失效
    meta={"preserve_all": True},
))

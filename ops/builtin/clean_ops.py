# -*- coding: utf-8 -*-
"""清洗域算子：单位换算 / 文本规范化 / 类别归一 / 模糊去重。

这四类此前只有"精确去重 + trim + 字典替换"，是清洗能力的主要空白：
金额混用「1.2万 / ¥1,234 / 100万元」、全半角与零宽字符、同义类别、
以及「XX置业有限公司 / XX置业公司」这类录入变体。
"""
from __future__ import annotations

import difflib
import html
import re
import unicodedata
from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import (
    TEXT_NORMALIZE_ACTIONS, UNIT_KINDS, UNIT_MULTIPLIERS, UNIT_TARGETS,
)
from ops.errors import RuleValidationError
from ops.primitives import as_cols
from ops.registry import OpSpec, ParamSpec, register_op

_NUMBER_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([^\d\s.,+-]*)\s*$")
_CURRENCY_RE = re.compile(r"[¥￥$,\s，]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COMPANY_SUFFIX_RE = re.compile(r"(有限责任公司|股份有限公司|有限公司|股份公司|公司|集团|厂|店)$")
_PUNCT_RE = re.compile(r"[\s\(\)（）\[\]【】,，.。·、\-—_/\\'\"]+")


# ------------------------------------------------------------
# convert_unit
# ------------------------------------------------------------
def _parse_number_with_unit(value: Any, multipliers: Dict[str, float]) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return float("nan")
    raw = str(value).strip()
    if not raw:
        return float("nan")
    cleaned = _CURRENCY_RE.sub("", raw)
    m = _NUMBER_RE.match(cleaned)
    if not m:
        return float("nan")
    number = float(m.group(1))
    unit = m.group(2).strip()
    if unit and unit not in multipliers:
        # 「万」「亿」可能被粘连（如 1.2万亿元），按最长后缀再尝试一次
        for key in sorted(multipliers, key=len, reverse=True):
            if unit.endswith(key):
                return number * multipliers[key]
        return float("nan")
    return number * (multipliers.get(unit, 1.0) if unit else 1.0)


def apply_convert_unit(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        raise RuleValidationError(["convert_unit 缺少 columns"])
    for col in cols:
        if col not in df.columns:
            raise RuleValidationError([f"convert_unit 列不存在：{col}"])
    kind = str(step.get("kind") or "money")
    multipliers = dict(UNIT_MULTIPLIERS)
    if isinstance(step.get("custom_multipliers"), dict):
        multipliers.update({str(k): float(v) for k, v in step["custom_multipliers"].items()})
    if kind in UNIT_TARGETS:
        multipliers.setdefault(UNIT_TARGETS[kind], 1.0)
    keep_raw = bool(step.get("keep_raw"))
    suffix = str(step.get("raw_suffix") or "_raw")
    frame = df.copy()
    changed = 0
    for col in cols:
        parsed = frame[col].map(lambda v: _parse_number_with_unit(v, multipliers))
        if keep_raw:
            raw_name = f"{col}{suffix}"
            frame[raw_name] = frame[col]
        changed += int(parsed.notna().sum())
        frame[col] = parsed
    target = UNIT_TARGETS.get(kind, str(step.get("target") or kind))
    return frame, f"单位换算 {','.join(cols)} → {target}（{changed} 个值可解析）"


def _unit_outputs(step: Dict[str, Any]) -> List[str]:
    if not step.get("keep_raw"):
        return []
    suffix = str(step.get("raw_suffix") or "_raw")
    return [f"{c}{suffix}" for c in as_cols(step.get("columns") or step.get("column"))]


def _unit_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("columns") or step.get("column"))


def _validate_unit(step, known, tag, errors) -> None:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        errors.append(f"{tag}.columns 不能为空")
    for col in cols:
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    if step.get("kind") is not None and step["kind"] not in UNIT_KINDS:
        errors.append(f"{tag}.kind 非法，可选 {list(UNIT_KINDS)}")
    custom = step.get("custom_multipliers")
    if custom is not None and (not isinstance(custom, dict)
                              or any(not isinstance(k, str) for k in custom)):
        errors.append(f"{tag}.custom_multipliers 必须是「单位名 → 乘数」对象")


# ------------------------------------------------------------
# text_normalize
# ------------------------------------------------------------
def _normalize_text(value: Any, actions: List[str]) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return value
    text = str(value)
    if "nfkc" in actions:
        text = unicodedata.normalize("NFKC", text)
    if "bom" in actions:
        text = text.lstrip("\ufeff")
    if "zero_width" in actions:
        text = _ZERO_WIDTH_RE.sub("", text)
    if "html" in actions:
        text = html.unescape(text)
    if "control" in actions:
        text = _CONTROL_RE.sub("", text)
    if "whitespace" in actions:
        text = re.sub(r"\s+", " ", text).strip()
    return text


def apply_text_normalize(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        raise RuleValidationError(["text_normalize 缺少 columns"])
    for col in cols:
        if col not in df.columns:
            raise RuleValidationError([f"text_normalize 列不存在：{col}"])
    actions = [str(a) for a in (step.get("actions") or TEXT_NORMALIZE_ACTIONS)]
    frame = df.copy()
    for col in cols:
        frame[col] = frame[col].map(lambda v: _normalize_text(v, actions))
    return frame, f"文本规范化 {','.join(cols)}（{'/'.join(actions)}）"


def _validate_text_normalize(step, known, tag, errors) -> None:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        errors.append(f"{tag}.columns 不能为空")
    for col in cols:
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    actions = step.get("actions")
    if actions is not None:
        if not isinstance(actions, list):
            errors.append(f"{tag}.actions 必须是数组")
        else:
            bad = [a for a in actions if a not in TEXT_NORMALIZE_ACTIONS]
            if bad:
                errors.append(f"{tag}.actions 含非法项 {bad}，可选 {list(TEXT_NORMALIZE_ACTIONS)}")


# ------------------------------------------------------------
# category_map
# ------------------------------------------------------------
def apply_category_map(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    col = str(step.get("column", ""))
    if col not in df.columns:
        raise RuleValidationError([f"category_map 列不存在：{col}"])
    mapping = step.get("mapping") if isinstance(step.get("mapping"), dict) else {}
    min_count = int(step.get("min_count") or 0)
    other = str(step.get("other_label") or "其他")
    case_sensitive = bool(step.get("case_sensitive", True))
    frame = df.copy()

    def convert(value: Any) -> Any:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
        key = str(value) if case_sensitive else str(value).lower()
        if key in mapping:
            return mapping[key]
        if not case_sensitive:
            for k, v in mapping.items():
                if str(k).lower() == key:
                    return v
        return value

    # 显式同义词映射优先；映射产生的标准值不再参与「罕见类别」判定，
    # 否则「北京市 → 北京」会因只出现一次又被覆盖成「其他」。
    protected = {str(v) for v in mapping.values()}
    if mapping:
        frame[col] = frame[col].map(convert)
    merged = 0
    if min_count > 1:
        counts = frame[col].value_counts(dropna=False)
        rare = {
            k for k, n in counts.items()
            if n < min_count and not pd.isna(k) and str(k) not in protected
        }
        merged = int(frame[col].isin(rare).sum())
        frame.loc[frame[col].isin(rare), col] = other
    return frame, (
        f"类别归一 {col}（{len(mapping)} 条映射"
        + (f"，罕见类别归并 {merged} 行 → {other}" if min_count > 1 else "")
        + "）"
    )


def _validate_category_map(step, known, tag, errors) -> None:
    col = step.get("column")
    if not isinstance(col, str) or not col:
        errors.append(f"{tag}.column 必须是列名")
    elif known and col not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {col!r}")
    mapping = step.get("mapping")
    if mapping is not None and not isinstance(mapping, dict):
        errors.append(f"{tag}.mapping 必须是对象")
    mc = step.get("min_count")
    if mc is not None and (not isinstance(mc, int) or isinstance(mc, bool) or mc < 1):
        errors.append(f"{tag}.min_count 必须是正整数")


# ------------------------------------------------------------
# dedupe_fuzzy
# ------------------------------------------------------------
def _norm_key(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _COMPANY_SUFFIX_RE.sub("", text)
    return _PUNCT_RE.sub("", text).lower()


def apply_dedupe_fuzzy(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        raise RuleValidationError(["dedupe_fuzzy 缺少 columns"])
    for col in cols:
        if col not in df.columns:
            raise RuleValidationError([f"dedupe_fuzzy 列不存在：{col}"])
    keep = str(step.get("keep") or "first")
    similarity = step.get("similarity")
    keys = df[cols].astype(str).agg("|".join, axis=1) if len(cols) > 1 else df[cols[0]]
    keys = keys.map(_norm_key)
    if similarity is not None:
        threshold = float(similarity)
        if not 0 < threshold <= 1:
            raise RuleValidationError(["dedupe_fuzzy.similarity 必须在 (0,1]"])
        groups: Dict[str, str] = {}
        canonical: List[str] = []
        for key in keys:
            hit = difflib.get_close_matches(key, canonical, n=1, cutoff=threshold)
            groups[key] = hit[0] if hit else key
            if not hit:
                canonical.append(key)
        keys = keys.map(groups)
    before = len(df)
    frame = df.assign(_dedupe_key=keys.values).drop_duplicates(
        subset=["_dedupe_key"], keep=keep
    ).drop(columns=["_dedupe_key"])
    return frame, f"模糊去重 {','.join(cols)}（删除 {before - len(frame)} 行）"


def _fuzzy_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("columns") or step.get("column"))


def _validate_dedupe_fuzzy(step, known, tag, errors) -> None:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        errors.append(f"{tag}.columns 不能为空")
    for col in cols:
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    if step.get("keep") is not None and step["keep"] not in ("first", "last", "all"):
        errors.append(f"{tag}.keep 必须是 first / last / all")
    sim = step.get("similarity")
    if sim is not None and (not isinstance(sim, (int, float)) or isinstance(sim, bool)
                           or not 0 < float(sim) <= 1):
        errors.append(f"{tag}.similarity 必须在 (0,1]")


register_op(OpSpec(
    name="convert_unit", layer=1, role="transform", execute=apply_convert_unit,
    validate=_validate_unit, refs=_unit_refs, outputs=_unit_outputs,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),
            ParamSpec("kind", str, enum=UNIT_KINDS)),
    summary="把「1.2万 / ¥1,234 / 120㎡」等混合写法统一换算到元或平方米",
    prompt_example={"op": "convert_unit", "columns": ["<金额列>"], "kind": "money", "keep_raw": True},
))
register_op(OpSpec(
    name="text_normalize", layer=1, role="transform", execute=apply_text_normalize,
    validate=_validate_text_normalize, refs=_unit_refs,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),
            ParamSpec("actions", list)),
    summary="Unicode NFKC / 全半角、零宽字符、BOM、HTML 转义、控制字符与空白归一",
    prompt_example={"op": "text_normalize", "columns": ["<文本列>"], "actions": ["nfkc", "zero_width", "whitespace"]},
))
register_op(OpSpec(
    name="category_map", layer=1, role="transform", execute=apply_category_map,
    validate=_validate_category_map, refs=lambda s: [s.get("column")],
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("mapping", dict), ParamSpec("min_count", int)),
    summary="类别同义词归并 + 罕见类别归入「其他」",
    prompt_example={"op": "category_map", "column": "<类别列>", "mapping": {"北京市": "北京"}, "min_count": 10},
))
register_op(OpSpec(
    name="dedupe_fuzzy", layer=1, role="filter", execute=apply_dedupe_fuzzy,
    validate=_validate_dedupe_fuzzy, refs=_fuzzy_refs, preserve_refs=_fuzzy_refs,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),),
    summary="按规范化键 / 相似度做近似去重（应对「XX置业有限公司」变体）",
    prompt_example={"op": "dedupe_fuzzy", "columns": ["<名称列>"], "similarity": 0.9},
))

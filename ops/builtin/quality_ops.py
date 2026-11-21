# -*- coding: utf-8 -*-
"""质量与合规算子：assert / profile / mask_pii。

- ``assert``   业务规则与跨列一致性校验（可报错 / 打标 / 剔除）；
- ``profile``  列级数据画像（缺失率、唯一性、类型、极值），而不只是 describe；
- ``mask_pii`` 身份证 / 手机号 / 姓名 / 邮箱 / 银行卡脱敏。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ops.conditions import build_mask, describe_condition
from ops.constants import MASK_MODES, PII_KINDS, as_list
from ops.errors import RuleValidationError
from ops.primitives import as_cols, numeric
from ops.registry import OpSpec, ParamSpec, register_op

ASSERT_ACTIONS = ("error", "flag", "drop")
PROFILE_COLUMNS = (
    "列名", "类型", "行数", "非空数", "缺失数", "缺失率(%)",
    "唯一值数", "示例值", "最小值", "最大值", "均值",
)

_PII_PATTERNS = {
    "phone": re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"),
    "id_card": re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)"),
    "bank_card": re.compile(r"(?<!\d)(\d{16,19})(?!\d)"),
    "email": re.compile(r"([\w.+-]+@[\w-]+\.[\w.-]+)"),
    "name": re.compile(r"([\u4e00-\u9fa5]{2,4})"),
}


# ------------------------------------------------------------
# assert
# ------------------------------------------------------------
def _assert_checks(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = step.get("checks")
    if isinstance(checks, list) and checks:
        return [c for c in checks if isinstance(c, dict)]
    if step.get("column"):
        return [{
            "column": step.get("column"),
            "operator": step.get("operator"),
            "value": step.get("value"),
        }]
    return []


def apply_assert(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    checks = _assert_checks(step)
    if not checks:
        raise RuleValidationError(["assert 缺少 checks / 单条件"])
    action = str(step.get("action") or "flag")
    into = str(step.get("into") or "校验通过")
    frame = df.copy()
    violation = pd.Series(False, index=df.index)
    details: List[str] = []
    for check in checks:
        ok = build_mask(frame, check).fillna(False)
        bad = ~ok
        violation |= bad
        details.append(f"{describe_condition(check)}：不通过 {int(bad.sum())} 行")
    total = int(violation.sum())
    if action == "error" and total:
        raise RuleValidationError(["业务规则校验失败：", *details])
    if action == "drop":
        frame = frame[~violation].copy()
        return frame, f"规则校验剔除 {total}/{len(df)} 行（{'；'.join(details)}）"
    frame[into] = ~violation
    return frame, f"规则校验打标 {into}：通过 {len(df) - total}/{len(df)} 行（{'；'.join(details)}）"


def _assert_refs(step: Dict[str, Any]) -> List[str]:
    cols: List[str] = []
    for check in _assert_checks(step):
        for cond in as_list(check.get("conditions")) if check.get("conditions") else [check]:
            if isinstance(cond, dict) and cond.get("column"):
                cols.append(str(cond["column"]))
    return cols


def _assert_outputs(step: Dict[str, Any]) -> List[str]:
    if str(step.get("action") or "flag") == "flag":
        return [str(step.get("into") or "校验通过")]
    return []


def _validate_assert(step, known, tag, errors) -> None:
    checks = _assert_checks(step)
    if not checks:
        errors.append(f"{tag} 需要 checks 数组或 column/operator/value 单条件")
    for i, check in enumerate(checks):
        conds = check.get("conditions") if isinstance(check.get("conditions"), list) else [check]
        for cond in conds:
            if not isinstance(cond, dict) or not cond.get("column"):
                errors.append(f"{tag}.checks[{i}] 缺少 column")
            elif known and cond["column"] not in known:
                errors.append(f"{tag}.checks[{i}] 引用了不存在的列 {cond['column']!r}")
            if isinstance(cond, dict) and cond.get("operator") not in (
                "eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains",
                "startswith", "endswith", "regex", "in", "not_in", "between",
                "notna", "isna",
            ):
                errors.append(f"{tag}.checks[{i}].operator 非法")
    if step.get("action") is not None and step["action"] not in ASSERT_ACTIONS:
        errors.append(f"{tag}.action 非法，可选 {list(ASSERT_ACTIONS)}")


# ------------------------------------------------------------
# profile
# ------------------------------------------------------------
def apply_profile(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = [c for c in as_cols(step.get("columns")) if c in df.columns] or list(df.columns)
    rows: List[Dict[str, Any]] = []
    total = len(df)
    for col in cols:
        s = df[col]
        non_null = int(s.notna().sum())
        try:
            unique = int(s.nunique(dropna=True))
        except TypeError:  # 不可哈希类型
            unique = -1
        sample = next((str(v)[:60] for v in s.dropna().head(3).tolist()), "")
        num = numeric(s)
        row: Dict[str, Any] = {
            "列名": col, "类型": str(s.dtype), "行数": total,
            "非空数": non_null, "缺失数": total - non_null,
            "缺失率(%)": round((total - non_null) / total * 100, 2) if total else 0.0,
            "唯一值数": unique, "示例值": sample,
            "最小值": "", "最大值": "", "均值": "",
        }
        if num.notna().any():
            row["最小值"] = round(float(num.min()), 4)
            row["最大值"] = round(float(num.max()), 4)
            row["均值"] = round(float(num.mean()), 4)
        rows.append(row)
    return pd.DataFrame(rows, columns=list(PROFILE_COLUMNS)), (
        f"列画像：{len(rows)} 列 / {total} 行（缺失率、唯一性、类型、极值）"
    )


def _profile_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("columns"))


def _profile_outputs(step: Dict[str, Any]) -> List[str]:
    return list(PROFILE_COLUMNS)


# ------------------------------------------------------------
# mask_pii
# ------------------------------------------------------------
def _mask_value(text: str, kind: str, mode: str, salt: str) -> str:
    if mode == "hash":
        return "h_" + hashlib.sha256((salt + text).encode("utf-8")).hexdigest()[:12]
    if mode == "full":
        return "*" * len(text)
    if len(text) <= 2:
        return text[0] + "*" if len(text) == 2 else "*"
    if kind == "email":
        name, _, domain = text.partition("@")
        return (name[:1] + "***@" + domain) if domain else text[:1] + "***"
    if kind in ("phone", "id_card", "bank_card"):
        return text[:3] + "*" * (len(text) - 7) + text[-4:]
    return text[:1] + "*" * (len(text) - 2) + text[-1:]


def apply_mask_pii(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = as_cols(step.get("columns"))
    if not cols:
        raise RuleValidationError(["mask_pii 缺少 columns"])
    for col in cols:
        if col not in df.columns:
            raise RuleValidationError([f"mask_pii 列不存在：{col}"])
    kinds = [str(k) for k in (step.get("kinds") or [])]
    if not kinds or "auto" in kinds:
        kinds = ["phone", "id_card", "bank_card", "email", "name"]
    mode = str(step.get("mode") or "partial")
    salt = str(step.get("salt") or "")
    keep_raw = bool(step.get("keep_raw"))
    frame = df.copy()
    hits = 0

    def convert(value: Any) -> Any:
        nonlocal hits
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
        text = str(value)
        out = text
        for kind in kinds:
            pattern = _PII_PATTERNS.get(kind)
            if pattern is None:
                continue
            def repl(m: "re.Match[str]") -> str:
                nonlocal hits
                hits += 1
                return _mask_value(m.group(1), kind, mode, salt)
            out = pattern.sub(repl, out)
        return out

    for col in cols:
        if keep_raw:
            frame[f"{col}_raw"] = frame[col]
        frame[col] = frame[col].map(convert)
    return frame, f"敏感信息脱敏 {','.join(cols)}（{mode}，命中 {hits} 处）"


def _mask_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("columns"))


def _mask_outputs(step: Dict[str, Any]) -> List[str]:
    if not step.get("keep_raw"):
        return []
    return [f"{c}_raw" for c in as_cols(step.get("columns"))]


def _validate_mask(step, known, tag, errors) -> None:
    cols = as_cols(step.get("columns"))
    if not cols:
        errors.append(f"{tag}.columns 不能为空")
    for col in cols:
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    kinds = step.get("kinds")
    if kinds is not None:
        if not isinstance(kinds, list):
            errors.append(f"{tag}.kinds 必须是数组")
        else:
            bad = [k for k in kinds if k not in PII_KINDS]
            if bad:
                errors.append(f"{tag}.kinds 含非法项 {bad}，可选 {list(PII_KINDS)}")
    if step.get("mode") is not None and step["mode"] not in MASK_MODES:
        errors.append(f"{tag}.mode 非法，可选 {list(MASK_MODES)}")


register_op(OpSpec(
    name="assert", layer=1, role="output", execute=apply_assert,
    validate=_validate_assert, refs=_assert_refs, outputs=_assert_outputs,
    params=(ParamSpec("checks", list), ParamSpec("action", str, enum=ASSERT_ACTIONS)),
    summary="业务规则 / 跨列一致性校验（报错、打标或剔除违规行）",
    prompt_example={"op": "assert", "checks": [{"column": "<金额列>", "operator": "gt", "value": 0}], "action": "flag"},
))
register_op(OpSpec(
    name="profile", layer=2, role="analysis", execute=apply_profile,
    refs=_profile_refs, outputs=_profile_outputs,
    params=(ParamSpec("columns", list, column=True, multi=True),),
    summary="列级数据画像：缺失率 / 唯一值 / 类型 / 极值 / 示例值", task_hint="summary",
    prompt_example={"op": "profile"},
))
register_op(OpSpec(
    name="mask_pii", layer=1, role="transform", execute=apply_mask_pii,
    validate=_validate_mask, refs=_mask_refs, outputs=_mask_outputs,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),
            ParamSpec("mode", str, enum=MASK_MODES)),
    summary="身份证 / 手机号 / 姓名 / 邮箱 / 银行卡脱敏（打码或哈希）",
    prompt_example={"op": "mask_pii", "columns": ["<手机号列>"], "kinds": ["phone"], "mode": "partial"},
))

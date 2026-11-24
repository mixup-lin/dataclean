# -*- coding: utf-8 -*-
"""值变换：cast / fill / string / split / concat / date_part / bin / derive / expr / assign / rank。"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

from ops.conditions import check_condition, condition_mask
from ops.constants import (
    BIN_METHODS, CAST_TYPES, DATE_PARTS, DATE_PART_LABELS, DERIVE_OPERATORS,
    FILL_METHODS, RANK_METHODS, STRING_ACTIONS, as_list,
)
from ops.datetime_utils import to_datetime_series
from ops.errors import RuleValidationError
from ops.primitives import as_cols, existing_cols, finite, numeric, require_columns
from ops.registry import OpSpec, ParamSpec, register_op


# ------------------------------------------------------------
# 实现
# ------------------------------------------------------------
def apply_cast(df, step, frames=None, ctx=None):
    cols = existing_cols(df, as_list(step.get("columns")) + as_list(step.get("column")))
    if not cols:
        raise RuleValidationError(["cast 列不存在"])
    to = str(step.get("to"))
    frame = df.copy()
    for col in cols:
        if to == "int":
            frame[col] = numeric(frame[col]).round().astype("Int64")
        elif to == "float":
            frame[col] = numeric(frame[col])
        elif to == "str":
            frame[col] = frame[col].astype("string")
        elif to == "datetime":
            frame[col] = pd.to_datetime(frame[col], errors="coerce", format="mixed")
        elif to == "category":
            frame[col] = frame[col].astype("category")
        elif to == "bool":
            truthy = {"1", "true", "yes", "y", "是", "真", "t"}
            frame[col] = frame[col].astype("string").str.strip().str.lower().isin(truthy)
        else:
            raise RuleValidationError([f"未知类型：{to}"])
    return frame, f"{len(cols)} 列转 {to}"


def apply_fill(df, step, frames=None, ctx=None):
    cols = existing_cols(df, as_list(step.get("columns")) + as_list(step.get("column")))
    if not cols:
        raise RuleValidationError(["fill 列不存在"])
    method = str(step.get("method"))
    frame = df.copy()
    if method == "drop":
        before = len(frame)
        frame = frame.dropna(subset=cols)
        return frame, f"删除 {cols} 缺失行 {before - len(frame)} 行"
    if method == "value":
        if "value" not in step:
            raise RuleValidationError(["fill method=value 需要 value"])
        frame[cols] = frame[cols].fillna(step["value"])
    elif method == "zero":
        for col in cols:
            frame[col] = frame[col].fillna(0)
    elif method in ("mean", "median"):
        for col in cols:
            num = numeric(frame[col])
            if num.notna().any():
                frame[col] = frame[col].fillna(num.mean() if method == "mean" else num.median())
    elif method == "mode":
        for col in cols:
            modes = frame[col].mode(dropna=True)
            if not modes.empty:
                frame[col] = frame[col].fillna(modes.iloc[0])
    elif method in ("ffill", "bfill"):
        frame[cols] = frame[cols].ffill() if method == "ffill" else frame[cols].bfill()
    else:
        raise RuleValidationError([f"未知填充方式：{method}"])
    filled = int(frame[cols].isna().sum().sum())
    return frame, f"{method} 填充 {cols}（剩余缺失 {filled}）"


def apply_string(df, step, frames=None, ctx=None):
    column = str(step.get("column"))
    require_columns(df, [column], "string")
    action = str(step.get("action"))
    into = step.get("into") or column
    text = df[column].astype("string")
    if action == "strip":
        out = text.str.strip()
    elif action == "lower":
        out = text.str.lower()
    elif action == "upper":
        out = text.str.upper()
    elif action == "title":
        out = text.str.title()
    elif action == "replace":
        out = text.str.replace(
            str(step.get("pattern", "")), str(step.get("repl", "")),
            regex=bool(step.get("regex", False)),
        )
    elif action == "regex_extract":
        pattern = str(step.get("pattern", ""))
        if not pattern:
            raise RuleValidationError(["regex_extract 需要 pattern"])
        out = text.str.extract(pattern, expand=False)
    elif action == "slice":
        start = int(step.get("start", 0))
        stop = step.get("stop")
        out = text.str.slice(start, int(stop) if isinstance(stop, int) else None)
    elif action == "pad":
        out = text.str.zfill(int(step.get("width", 6)))
    elif action == "cat":
        out = str(step.get("prefix", "")) + text.fillna("") + str(step.get("suffix", ""))
    else:
        raise RuleValidationError([f"未知字符串动作：{action}"])
    frame = df.copy()
    frame[into] = pd.Series(out, index=frame.index).fillna("")
    return frame, f"文本 {action} → {into}"


def apply_replace(df, step, frames=None, ctx=None):
    """值替换：把 mapping 应用到指定列（缺省全部列），用于口径统一。"""
    mapping = step.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        if "value" in step:
            mapping = {step.get("value"): step.get("replacement", "")}
    if not isinstance(mapping, dict) or not mapping:
        raise RuleValidationError(["replace 需要非空 mapping（或 value + replacement）"])
    cols = existing_cols(df, as_list(step.get("columns")) + as_list(step.get("column")))
    targets = cols or [str(c) for c in df.columns]
    str_map = {str(k): v for k, v in mapping.items()}
    frame = df.copy()
    changed = 0
    for col in targets:
        before = frame[col]
        replaced = before.replace(mapping)
        if replaced.equals(before):  # 字符串列 + 数值键等类型不匹配时按字符串再试一次
            as_text = before.astype("string")
            mapped = as_text.replace(str_map)
            if not mapped.equals(as_text):
                replaced = mapped
        if not replaced.equals(before):
            changed += 1
        frame[col] = replaced
    return frame, f"值替换 {len(mapping)} 项（命中 {changed} 列）"


def apply_split(df, step, frames=None, ctx=None):
    column = str(step.get("column"))
    require_columns(df, [column], "split")
    into = as_cols(step.get("into"))
    if not into:
        raise RuleValidationError(["split 需要 into"])
    sep = str(step.get("sep", "-"))
    parts = df[column].astype("string").str.split(sep, n=len(into) - 1, expand=True)
    frame = df.copy()
    for i, name in enumerate(into):
        frame[name] = parts[i] if i < parts.shape[1] else ""
    return frame, f"拆分 {column} → {','.join(into)}"


def apply_concat(df, step, frames=None, ctx=None):
    cols = existing_cols(df, step.get("columns"))
    if len(cols) < 2:
        raise RuleValidationError(["concat 至少需要两个存在的列"])
    into = step.get("into") or "_".join(cols)
    sep = str(step.get("sep", "-"))
    out = df[cols[0]].astype("string").fillna("")
    for col in cols[1:]:
        out = out + sep + df[col].astype("string").fillna("")
    frame = df.copy()
    frame[into] = out
    return frame, f"合并 {len(cols)} 列 → {into}"


def _bucket_series(dt: pd.Series, parts: Sequence[str]) -> Optional[pd.Series]:
    """把多个日期分量拼成可跨年排序的组合时间桶。

    quarter/week 使用紧凑连接（2026Q01 / 2026W12），其余用 ``-`` 连接，
    保证字符串升序与时间升序一致；无法解析的日期保持 <NA>。
    """
    segments: List[Tuple[str, str]] = []
    if "year" in parts:
        segments.append(("", dt.dt.year.astype("Int64").astype("string").str.zfill(4)))
    if "quarter" in parts:
        segments.append(("Q", dt.dt.quarter.astype("Int64").astype("string").str.zfill(2)))
    elif "month" in parts:
        segments.append(("", dt.dt.month.astype("Int64").astype("string").str.zfill(2)))
    if "week" in parts:
        segments.append(("W", dt.dt.isocalendar().week.astype("Int64").astype("string").str.zfill(2)))
    if "day" in parts:
        segments.append(("", dt.dt.day.astype("Int64").astype("string").str.zfill(2)))
    if len(segments) < 2:
        return None
    composed: Optional[pd.Series] = None
    for prefix, series in segments:
        segment = (pd.Series(prefix, index=series.index, dtype="string") + series) if prefix else series
        if composed is None:
            composed = segment
        elif prefix:
            composed = composed + segment
        else:
            composed = composed + "-" + segment
    return composed


def apply_date_part(df, step, frames=None, ctx=None):
    column = str(step.get("column"))
    require_columns(df, [column], "date_part")
    dt = to_datetime_series(df[column])
    frame = df.copy()
    made: List[str] = []
    parts = [str(p) for p in as_cols(step.get("parts"))]
    for part in parts:
        label = DATE_PART_LABELS.get(part)
        if not label:
            continue
        if part == "year":
            values = dt.dt.year
        elif part == "quarter":
            values = dt.dt.quarter
        elif part == "month":
            values = dt.dt.month
        elif part == "week":
            values = dt.dt.isocalendar().week
        elif part == "day":
            values = dt.dt.day
        elif part == "weekday":
            values = dt.dt.weekday
        else:
            values = dt.dt.hour
        out_col = f"{column}_{label}"
        frame[out_col] = pd.Series(values, index=frame.index).astype("Int64")
        made.append(out_col)
    if not made:
        raise RuleValidationError(["date_part 未生成任何分量"])
    bucket = step.get("bucket")
    if bucket:
        composed = _bucket_series(dt, parts)
        if composed is None:
            raise RuleValidationError(["date_part.bucket 至少需要两个可组合的分量（如 year + quarter）"])
        bucket_col = f"{column}_{bucket}"
        frame[bucket_col] = composed
        made.append(bucket_col)
    return frame, f"日期分量：{','.join(made)}（{int(dt.notna().sum())} 行可解析）"


def apply_bin(df, step, frames=None, ctx=None):
    column = str(step.get("column"))
    require_columns(df, [column], "bin")
    into = step.get("into") or f"{column}_分箱"
    bins = step.get("bins", 5)
    method = str(step.get("method") or "width")
    num = numeric(df[column])
    # 空数据 / 全缺失时 pd.cut 会抛「Cannot cut empty array」；直接产出空分箱列，
    # 保持列名契约稳定，不中断全流程。
    if num.dropna().empty:
        frame = df.copy()
        frame[into] = pd.Categorical([], categories=["缺失"], ordered=True)
        return frame, f"分箱 {into}：无有效数值，全部记为缺失"
    if isinstance(bins, list) and len(bins) >= 2:
        edges = [float(b) for b in bins]
        binned = pd.cut(num, bins=edges, include_lowest=True, precision=0)
        method = "自定义边界"
    elif method == "quantile":
        try:
            binned = pd.qcut(num, q=int(bins), duplicates="drop", precision=0)
        except (ValueError, IndexError):
            binned = pd.cut(num, bins=int(bins), precision=0)
            method = "宽度（分位失败回退）"
    else:
        binned = pd.cut(num, bins=int(bins), precision=0)
        method = "等宽"
    labels = binned.astype("string").fillna("缺失")
    frame = df.copy()
    frame[into] = labels
    frame[into] = pd.Categorical(
        frame[into], categories=[*binned.cat.categories.astype(str), "缺失"], ordered=True,
    )
    return frame, f"分箱 {into}：{method} {len(binned.cat.categories)} 段"


def apply_derive(df, step, frames=None, ctx=None):
    op = step["operator"]
    left = numeric(df[step["left"]])
    right_raw = step.get("right")
    if isinstance(right_raw, str) and right_raw in df.columns:
        right: Any = numeric(df[right_raw])
    else:
        right = float(right_raw) if right_raw is not None else 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        if op == "add":
            result = left + right
        elif op == "sub":
            result = left - right
        elif op == "mul":
            result = left * right
        elif op in ("ratio", "pct_change"):
            if isinstance(right, pd.Series):
                result = left / right.replace(0, np.nan)
            elif right == 0:
                result = left * np.nan
            else:
                result = left / right
        elif op == "round":
            result = left.round(int(step.get("digits", 2)))
        else:
            result = left / right
    frame = df.copy()
    frame[step["column"]] = finite(result)
    return frame, f"派生列 {step['column']}"


# ------------------------------------------------------------
# expr：安全算术表达式派生（支持描述里显式给出的任意公式）
# ------------------------------------------------------------
# 表达式里的列用 c0 / c1 引用 operands，避免真实列名里的括号/空格破坏语法。
_EXPR_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
}
_EXPR_UNARY = (ast.UAdd, ast.USub)
_EXPR_FUNCS = {
    "abs": np.abs, "round": np.round, "min": np.minimum,
    "max": np.maximum, "sqrt": np.sqrt, "log": np.log, "exp": np.exp,
}


def parse_expression(text: Any) -> Tuple[ast.AST, Set[int]]:
    """解析受限算术表达式，返回 AST 与引用到的操作数下标集合。

    只允许四则运算、括号、数字常量、``@N`` 列引用与白名单函数；
    任何属性访问、下标、比较、布尔运算等都会抛 ``ValueError``。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("expr.expression 不能为空")
    tree = ast.parse(text.strip(), mode="eval")
    indices: Set[int] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _EXPR_BINOPS:
                raise ValueError(f"expr 不支持运算符 {type(node.op).__name__}")
            visit(node.left)
            visit(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _EXPR_UNARY):
                raise ValueError("expr 只支持正负号")
            visit(node.operand)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("expr 只接受数字常量")
        elif isinstance(node, ast.Name):
            match = re.fullmatch(r"c(\d+)", node.id)
            if not match:
                raise ValueError("expr 的列引用必须写成 c0 / c1 形式")
            indices.add(int(match.group(1)))
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _EXPR_FUNCS:
                raise ValueError(f"expr 不支持函数 {getattr(node.func, 'id', '?')}")
            if node.keywords:
                raise ValueError("expr 函数不支持关键字参数")
            for arg in node.args:
                visit(arg)
        else:
            raise ValueError(f"expr 不支持语法 {type(node).__name__}")

    visit(tree.body)
    return tree.body, indices


def _eval_expr(node: ast.AST, values: List[Any]) -> Any:
    if isinstance(node, ast.BinOp):
        return _EXPR_BINOPS[type(node.op)](_eval_expr(node.left, values), _eval_expr(node.right, values))
    if isinstance(node, ast.UnaryOp):
        value = _eval_expr(node.operand, values)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values[int(node.id[1:])]
    if isinstance(node, ast.Call):
        args = [_eval_expr(arg, values) for arg in node.args]
        # min/max 允许多参数；其余一元函数只接第一个参数
        if len(args) > 1 and node.func.id in ("min", "max"):
            result = args[0]
            for arg in args[1:]:
                result = _EXPR_FUNCS[node.func.id](result, arg)
            return result
        return _EXPR_FUNCS[node.func.id](args[0])
    raise ValueError(f"expr 不支持语法 {type(node).__name__}")


def apply_expr(df, step, frames=None, ctx=None):
    operands = [str(c) for c in (step.get("operands") or [])]
    node, indices = parse_expression(step.get("expression"))
    if any(index >= len(operands) for index in indices):
        raise RuleValidationError([
            f"expr.expression 引用了不存在的操作数（operands 共 {len(operands)} 项）"
        ])
    values: List[Any] = []
    for column in operands:
        if column in df.columns:
            values.append(numeric(df[column]))
        else:
            try:
                values.append(float(column))
            except (TypeError, ValueError):
                raise RuleValidationError([f"expr.operands 中的列 {column!r} 不存在"])
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        result = _eval_expr(node, values)
    if not isinstance(result, pd.Series):
        result = pd.Series(result, index=df.index, dtype="float64")
    frame = df.copy()
    frame[str(step["column"])] = finite(result)
    return frame, f"公式派生列 {step['column']} = {step.get('expression')}"


def _refs_expr(step):
    return [str(c) for c in (step.get("operands") or [])]


def _validate_expr(step, known, tag, errors):
    if not step.get("column"):
        errors.append(f"{tag}.column 不能为空")
    operands = step.get("operands")
    if not isinstance(operands, list) or not operands:
        errors.append(f"{tag}.operands 必须是非空数组")
        return
    try:
        _, indices = parse_expression(step.get("expression"))
    except (ValueError, SyntaxError) as exc:
        errors.append(f"{tag}.expression 非法：{exc}")
        return
    if any(index >= len(operands) for index in indices):
        errors.append(f"{tag}.expression 引用了超出 operands 范围的 cN")


def apply_assign(df, step, frames=None, ctx=None):
    rules = step.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RuleValidationError(["assign 需要 rules"])
    conditions: List[Any] = []
    values: List[Any] = []
    for item in rules:
        if not isinstance(item, dict):
            raise RuleValidationError(["assign.rules 项必须是对象"])
        cond = item.get("condition")
        mask = condition_mask(df, cond) if isinstance(cond, dict) else pd.Series(True, index=df.index)
        conditions.append(mask)
        values.append(item.get("value"))
    column = str(step.get("column"))
    frame = df.copy()
    frame[column] = np.select(conditions, values, default=step.get("default", "其他"))
    return frame, f"条件赋值 {column}（{len(rules)} 条规则）"


def apply_rank(df, step, frames=None, ctx=None):
    by = str(step.get("by"))
    require_columns(df, [by], "rank")
    into = step.get("into") or f"{by}_排名"
    method = str(step.get("method") or "average")
    ascending = bool(step.get("ascending", False))
    group_by = step.get("group_by")
    frame = df.copy()
    if isinstance(group_by, str) and group_by in frame.columns:
        frame[into] = frame.groupby(group_by, dropna=False)[by].rank(method=method, ascending=ascending)
        note = f"组内排名 {into}（按 {group_by}）"
    else:
        frame[into] = numeric(frame[by]).rank(method=method, ascending=ascending)
        note = f"排名 {into}（按 {by}{'升序' if ascending else '降序'}）"
    topn = step.get("topn")
    if isinstance(topn, int) and topn > 0:
        before = len(frame)
        frame = frame[frame[into] <= topn]
        note += f"，保留 Top{topn}：{len(frame)}/{before} 行"
    return frame, note


# ------------------------------------------------------------
# 输出列 / 输入列 / 校验
# ------------------------------------------------------------
def _out_col(step):
    col = step.get("column") or step.get("into")
    return [col] if isinstance(col, str) and col else []


def _out_bin(step):
    into = step.get("into")
    if isinstance(into, str) and into:
        return [into]
    col = step.get("column")
    return [f"{col}_分箱"] if isinstance(col, str) and col else []


def _out_string(step):
    into = step.get("into") or step.get("column")
    return [into] if isinstance(into, str) and into else []


def _out_date_part(step):
    col = step.get("column")
    out = []
    for part in as_list(step.get("parts")):
        label = DATE_PART_LABELS.get(str(part))
        if label and isinstance(col, str) and col:
            out.append(f"{col}_{label}")
    bucket = step.get("bucket")
    if bucket and isinstance(col, str) and col and len(out) >= 2:
        out.append(f"{col}_{bucket}")
    return out


def _out_split(step):
    return [str(c) for c in as_list(step.get("into")) if str(c).strip()]


def _out_concat(step):
    into = step.get("into")
    if isinstance(into, str) and into:
        return [into]
    cols = as_cols(step.get("columns"))
    return ["_".join(cols)] if cols else []


def _refs_left(step):
    return [step.get("left")] if step.get("left") else []


def _refs_replace(step):
    return as_list(step.get("columns")) + as_list(step.get("column"))


def _validate_replace(step, known, tag, errors):
    mapping = step.get("mapping")
    if mapping is None:
        if "value" not in step:
            errors.append(f"{tag} 需提供 mapping 或 value/replacement")
    elif not isinstance(mapping, dict) or not mapping:
        errors.append(f"{tag}.mapping 必须是非空对象")


def _refs_assign(step):
    refs = []
    for item in step.get("rules") or []:
        if isinstance(item, dict) and isinstance(item.get("condition"), dict):
            refs.append(item["condition"].get("column"))
    return refs


def _refs_rank(step):
    return [step.get("by")] + as_list(step.get("group_by"))


def _validate_cast(step, known, tag, errors):
    if step.get("to") not in CAST_TYPES:
        errors.append(f"{tag}.to 非法，可选 {list(CAST_TYPES)}")
    if not (step.get("column") or step.get("columns")):
        errors.append(f"{tag} 需提供 column 或 columns")


def _validate_fill(step, known, tag, errors):
    if step.get("method") not in FILL_METHODS:
        errors.append(f"{tag}.method 非法，可选 {list(FILL_METHODS)}")
    if step.get("method") == "value" and "value" not in step:
        errors.append(f"{tag} method=value 时必须提供 value")


def _validate_string(step, known, tag, errors):
    if step.get("action") not in STRING_ACTIONS:
        errors.append(f"{tag}.action 非法，可选 {list(STRING_ACTIONS)}")
    if step.get("action") in ("replace", "regex_extract") and not isinstance(step.get("pattern"), str):
        errors.append(f"{tag}.pattern 必须提供字符串")
    if step.get("action") == "slice" and not isinstance(step.get("start"), int):
        errors.append(f"{tag}.start 必须提供整数")


def _validate_date_part(step, known, tag, errors):
    parts = as_list(step.get("parts"))
    if not parts:
        errors.append(f"{tag}.parts 不能为空")
    for part in parts:
        if part not in DATE_PARTS:
            errors.append(f"{tag}.parts 含非法值 {part!r}")


def _validate_bin(step, known, tag, errors):
    if step.get("method") is not None and step["method"] not in BIN_METHODS:
        errors.append(f"{tag}.method 非法，可选 {list(BIN_METHODS)}")
    bins = step.get("bins")
    if bins is not None and not isinstance(bins, (int, list)):
        errors.append(f"{tag}.bins 必须是整数或边界数组")


def _validate_derive(step, known, tag, errors):
    if step.get("operator") not in DERIVE_OPERATORS:
        errors.append(f"{tag}.operator 非法算子")
    right = step.get("right")
    if right is not None and not isinstance(right, (str, int, float, bool)):
        errors.append(f"{tag}.right 必须是列名或数字")


def _validate_assign(step, known, tag, errors):
    rules = step.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append(f"{tag}.rules 必须是非空数组")
        return
    for i, item in enumerate(rules):
        if not isinstance(item, dict):
            errors.append(f"{tag}.rules[{i}] 必须是对象")
            continue
        cond = item.get("condition")
        if cond is not None and not isinstance(cond, dict):
            errors.append(f"{tag}.rules[{i}].condition 必须是对象")
        elif isinstance(cond, dict):
            check_condition(cond, f"{tag}.rules[{i}].condition", known, errors)
        if "value" not in item:
            errors.append(f"{tag}.rules[{i}] 缺少字段 value")


def _validate_rank(step, known, tag, errors):
    if step.get("method") is not None and step["method"] not in RANK_METHODS:
        errors.append(f"{tag}.method 非法，可选 {list(RANK_METHODS)}")
    topn = step.get("topn")
    if topn is not None and (not isinstance(topn, int) or topn < 1):
        errors.append(f"{tag}.topn 必须是正整数")
    group_by = step.get("group_by")
    if isinstance(group_by, str) and known and group_by not in known:
        errors.append(f"{tag}.group_by 引用了不存在的列 {group_by!r}")


register_op(OpSpec(
    name="cast", layer=1, role="transform", execute=apply_cast, validate=_validate_cast,
    params=(ParamSpec("to", str, required=True, enum=CAST_TYPES),),
    summary="列类型转换", task_hint="",
    prompt_example={"op": "cast", "columns": ["<列>"], "to": "float"},
))
register_op(OpSpec(
    name="fill", layer=1, role="transform", execute=apply_fill, validate=_validate_fill,
    params=(ParamSpec("method", str, required=True, enum=FILL_METHODS),),
    summary="缺失值填充/删除",
    prompt_example={"op": "fill", "columns": ["<列>"], "method": "median"},
))
register_op(OpSpec(
    name="replace", layer=1, role="transform", execute=apply_replace, validate=_validate_replace,
    refs=_refs_replace,
    summary="按字典替换取值（口径统一：是/否、别名归并）",
    prompt_example={"op": "replace", "columns": ["<列>"], "mapping": {"是": "Y", "否": "N"}},
))
register_op(OpSpec(
    name="string", layer=1, role="transform", execute=apply_string, validate=_validate_string,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("action", str, required=True, enum=STRING_ACTIONS)),
    outputs=_out_string, summary="字符串变换",
    prompt_example={"op": "string", "column": "<列>", "action": "strip", "into": "<新列>"},
))
register_op(OpSpec(
    name="split", layer=1, role="transform", execute=apply_split,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("into", list, required=True)),
    outputs=_out_split, summary="按分隔符拆分列",
    prompt_example={"op": "split", "column": "<列>", "sep": "-", "into": ["<列1>", "<列2>"]},
))
register_op(OpSpec(
    name="concat", layer=1, role="transform", execute=apply_concat,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),),
    outputs=_out_concat, summary="拼接多列为新列",
    prompt_example={"op": "concat", "columns": ["<列1>", "<列2>"], "sep": "-", "into": "<新列>"},
))
register_op(OpSpec(
    name="date_part", layer=1, role="transform", execute=apply_date_part, validate=_validate_date_part,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("parts", list, required=True),
            ParamSpec("bucket", str)),
    outputs=_out_date_part, produces_time_dim=True,
    summary="抽取日期分量；bucket 可把多分量拼成跨年安全的组合时间桶（如 year+quarter → 2026Q01）",
    prompt_example={"op": "date_part", "column": "<日期列>", "parts": ["year", "quarter"],
                    "bucket": "年季度"},
))
register_op(OpSpec(
    name="bin", layer=1, role="transform", execute=apply_bin, validate=_validate_bin,
    params=(ParamSpec("column", str, required=True, column=True),),
    outputs=_out_bin, summary="数值分箱", task_hint="distribution",
    prompt_example={"op": "bin", "column": "<数值列>", "bins": 5, "method": "width"},
))
register_op(OpSpec(
    name="derive", layer=1, role="transform", execute=apply_derive, validate=_validate_derive,
    params=(ParamSpec("column", str, required=True),
            ParamSpec("operator", str, required=True, enum=DERIVE_OPERATORS),
            ParamSpec("left", str, required=True, column=True)),
    outputs=_out_col, refs=_refs_left, summary="两列/列与常量派生新列",
    prompt_example={"op": "derive", "column": "<新列>", "operator": "ratio", "left": "<列>", "right": "<列>"},
))
register_op(OpSpec(
    name="expr", layer=1, role="transform", execute=apply_expr, validate=_validate_expr,
    params=(ParamSpec("column", str, required=True),
            ParamSpec("expression", str, required=True),
            ParamSpec("operands", list, required=True)),
    outputs=_out_col, refs=_refs_expr, summary="按显式公式派生新列（列用 c0/c1 引用 operands，支持四则运算与括号）",
    prompt_example={"op": "expr", "column": "成交单价", "expression": "c0 / c1",
                    "operands": ["成交价(元)", "面积(㎡)"]},
))
register_op(OpSpec(
    name="assign", layer=1, role="transform", execute=apply_assign, validate=_validate_assign,
    params=(ParamSpec("column", str, required=True),),
    outputs=_out_col, refs=_refs_assign, summary="按条件分段打标",
    prompt_example={"op": "assign", "column": "<新列>", "rules": [{"condition": {"column": "<列>", "operator": "gt", "value": 0}, "value": "高"}], "default": "低"},
))
register_op(OpSpec(
    name="rank", layer=1, role="analysis", execute=apply_rank, validate=_validate_rank,
    params=(ParamSpec("by", str, required=True, column=True),),
    outputs=_out_col, refs=_refs_rank, summary="排名 / TopN", task_hint="ranking",
    prompt_example={"op": "rank", "by": "<数值列>", "into": "<排名列>", "method": "dense", "topn": 10},
))

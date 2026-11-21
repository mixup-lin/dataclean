# -*- coding: utf-8 -*-
"""算子体系的枚举、常量与命名规则（单一事实源的"值域"部分）。

执行层、校验层、提示词生成、任务画像都从这里读取枚举与命名口径，
避免同一约束在多处硬编码、彼此漂移。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# ------------------------------------------------------------
# 聚合
# ------------------------------------------------------------
CORE_AGG = ("count", "mean", "sum", "min", "max", "median")
EXT_AGG = ("std", "var", "nunique", "first", "last", "quantile")
AGG_WHITELIST = CORE_AGG + EXT_AGG
GROUP_AGG_WHITELIST = CORE_AGG + ("std", "var", "nunique", "first", "last")
PIVOT_AGG_WHITELIST = CORE_AGG

# ------------------------------------------------------------
# 算子参数枚举
# ------------------------------------------------------------
FILTER_OPERATORS = (
    "eq", "ne", "gt", "gte", "lt", "lte",
    "contains", "not_contains", "startswith", "endswith", "regex",
    "in", "not_in", "between", "notna", "isna",
)
DERIVE_OPERATORS = ("add", "sub", "mul", "div", "ratio", "pct_change", "round")
LOGIC_OPERATORS = ("and", "or")
PCT_SCOPES = ("condition", "overall", "group")
JOIN_HOWS = ("inner", "left", "right", "outer")
CAST_TYPES = ("int", "float", "str", "datetime", "category", "bool")
FILL_METHODS = ("value", "mean", "median", "mode", "ffill", "bfill", "zero", "drop")
STRING_ACTIONS = (
    "strip", "lower", "upper", "title", "replace",
    "regex_extract", "slice", "pad", "cat",
)
DATE_PARTS = ("year", "quarter", "month", "week", "day", "weekday", "hour")
DATE_PART_LABELS = {
    "year": "年", "quarter": "季度", "month": "月", "week": "周",
    "day": "日", "weekday": "星期", "hour": "小时",
}
RANK_METHODS = ("average", "min", "max", "dense", "first")
DEDUPE_KEEP = ("first", "last", "all")
BIN_METHODS = ("width", "quantile")
DROPNA_HOWS = ("any", "all")
NORMALIZE_METHODS = ("minmax", "zscore", "rank_pct")
OUTLIER_METHODS = ("iqr", "zscore")
OUTLIER_ACTIONS = ("flag", "clip", "remove")
DATE_DIFF_UNITS = ("day", "hour", "minute", "month", "year")

# ------------------------------------------------------------
# 算子角色 / 任务类型（任务画像判定的值域）
# ------------------------------------------------------------
OP_ROLES = ("filter", "transform", "analysis", "output")
TASK_TYPES = (
    "proportion", "ranking", "trend", "distribution", "pivot",
    "join", "summary", "correlation", "detail",
)

# ------------------------------------------------------------
# 新增算子枚举
# ------------------------------------------------------------
DATE_FILTER_MODES = ("year", "quarter", "month", "range", "recent")
DATE_PARSE_ON_ERROR = ("coerce", "raise")
UNIT_KINDS = ("money", "area", "custom")
TEXT_NORMALIZE_ACTIONS = ("nfkc", "zero_width", "html", "control", "whitespace", "bom")
PII_KINDS = ("phone", "id_card", "name", "email", "bank_card", "auto")
MASK_MODES = ("partial", "full", "hash")
ROLLING_FUNCS = ("mean", "sum", "min", "max", "std", "median", "count")
RESAMPLE_FUNCS = ("mean", "sum", "count", "min", "max", "median", "first", "last")
CORR_METHODS = ("pearson", "spearman", "kendall")
HAVING_OPERATORS = ("gt", "gte", "lt", "lte", "eq", "ne")
SET_JOIN_HOWS = ("semi", "anti")
CROSS_COLUMN_OPERATORS = ("gt", "gte", "lt", "lte", "eq", "ne")
AGG_TOPN_MODES = ("top", "bottom")

# 单位换算：中文金额 / 面积常见写法 -> 乘数（目标单位见 UNIT_TARGETS）
UNIT_MULTIPLIERS: Dict[str, float] = {
    "元": 1.0, "块": 1.0,
    "万": 1e4, "万元": 1e4, "万块": 1e4,
    "亿": 1e8, "亿元": 1e8,
    "千": 1e3, "百": 1e2,
    "平方米": 1.0, "平米": 1.0, "㎡": 1.0, "m²": 1.0, "m2": 1.0,
    "亩": 666.6667, "公顷": 1e4, "平方千米": 1e6,
}
UNIT_TARGETS = {"money": "元", "area": "平方米"}

# ------------------------------------------------------------
# pct 算子产出的固定列名：分子 / 分母 / 占比（三口径共用，便于下游排序与筛选）
# ------------------------------------------------------------
PCT_NUM_COL = "分子"
PCT_DEN_COL = "分母"
PCT_DEFAULT_ALIAS = "占比"
PCT_COLUMNS = (PCT_NUM_COL, PCT_DEN_COL, PCT_DEFAULT_ALIAS)

# 透视列上限：列过多时按列合计取前 N，避免交叉表撑爆页面与报告
MAX_PIVOT_COLUMNS = 36


# ------------------------------------------------------------
# 纯函数辅助（值域/命名口径，执行层与校验层共用）
# ------------------------------------------------------------
def as_list(value: Any) -> List[Any]:
    """把标量/None 统一成列表，供参数展开使用。"""
    if isinstance(value, list):
        return list(value)
    return [value] if value is not None else []


# 频率别名的单一事实源：核心管道（etl_pipeline._normalize_freq）与算子层
# （normalize_freq_alias）共用，避免两处各写一份而漂移。
FREQ_ALIASES = {"M": "ME", "Q": "QE", "Y": "YE", "A": "YE", "H": "h", "T": "min"}
_FREQ_ALIASES = FREQ_ALIASES  # 兼容旧引用


def normalize_freq_alias(freq: str) -> str:
    """兼容 pandas 3.x 的月/季/年频率别名（M→ME、Q→QE、Y→YE）。"""
    raw = str(freq).strip()
    if raw in FREQ_ALIASES:
        return FREQ_ALIASES[raw]
    for old, new in (("M", "ME"), ("Q", "QE"), ("Y", "YE")):
        if len(raw) > 1 and raw.endswith(old) and raw[:-1].isdigit():
            return raw[:-1] + new
    return raw


def dedupe_name(name: str, used: set) -> str:
    """列名冲突时追加序号，保证 aggregate 产出的列名稳定且唯一。"""
    if name not in used:
        return name
    i = 2
    while f"{name}_{i}" in used:
        i += 1
    return f"{name}_{i}"


def pct_alias(step: Dict[str, Any]) -> str:
    alias = step.get("alias")
    return str(alias).strip() if isinstance(alias, str) and alias.strip() else PCT_DEFAULT_ALIAS


def aggregate_pairs(step: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """展开 aggregate 的 (列, 函数, 别名) 三元组。

    执行层与校验层共用同一口径，避免默认列名在两侧不一致导致后续步骤
    引用不到聚合结果。
    """
    cols = [str(c) for c in as_list(step.get("column"))]
    funcs = [str(f) for f in as_list(step.get("func"))] or ["count"]
    if len(funcs) == 1:
        pairs = [(c, funcs[0]) for c in cols]
    elif len(cols) == 1:
        pairs = [(cols[0], f) for f in funcs]
    elif len(funcs) == len(cols):
        pairs = list(zip(cols, funcs))
    else:
        pairs = [(cols[0], funcs[0])] if cols else []
    aliases = [str(a) for a in as_list(step.get("alias")) if str(a).strip()]
    used = {str(b) for b in as_list(step.get("by"))}
    out: List[Tuple[str, str, str]] = []
    for i, (col, func) in enumerate(pairs):
        alias = aliases[i] if i < len(aliases) else ""
        if not alias:
            alias = "条数" if func == "count" else f"{col}_{func}"
        if alias in used:
            alias = dedupe_name(f"{col}_{func}", used)
        used.add(alias)
        out.append((col, func, alias))
    return out

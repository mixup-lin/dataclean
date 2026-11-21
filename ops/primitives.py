# -*- coding: utf-8 -*-
"""L0 基础原子：与具体算子无关的 pandas 小工具。

这些原语被 L1/L2 算子复用（列解析、数值化、缺列校验、聚合函数包装、
占比取值等），是算子实现的公共地基。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ops.constants import MAX_PIVOT_COLUMNS
from ops.errors import RuleValidationError


def numeric(series: pd.Series) -> pd.Series:
    """尽力把序列转为数值，无法解析的值置为 NaN。"""
    return pd.to_numeric(series, errors="coerce")


def as_cols(value: Any) -> List[str]:
    """把标量/列表参数统一成"非空列名列表"。"""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)] if value not in (None, "") else []


def existing_cols(df: pd.DataFrame, value: Any) -> List[str]:
    """返回 value 中真正存在于 df 的列，保持顺序。"""
    return [c for c in as_cols(value) if c in df.columns]


def require_columns(df: pd.DataFrame, cols: Sequence[str], tag: str) -> None:
    """断言列存在，否则抛带上下文的校验异常。"""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise RuleValidationError([f"{tag} 列不存在：{missing}"])


def aggregate_func(col: str, func: str, step: Dict[str, Any]) -> Any:
    """把聚合函数名包装成 pandas NamedAgg，统一 count/quantile 的口径。"""
    if func == "count":
        return pd.NamedAgg(column=col, aggfunc=len)
    if func == "quantile":
        q = float(step.get("q", 0.5))
        return pd.NamedAgg(column=col, aggfunc=lambda s, q=q: s.quantile(q))
    return pd.NamedAgg(column=col, aggfunc=func)


def pct_values(df: pd.DataFrame, column: Optional[str]) -> pd.Series:
    """占比取数：指定列取数值（缺失记 0），否则每行计 1（条数占比）。"""
    if isinstance(column, str) and column in df.columns:
        return numeric(df[column]).fillna(0.0)
    return pd.Series(1.0, index=df.index)


def finite(series: pd.Series) -> pd.Series:
    """把 ±inf 归为 NaN，避免污染下游统计。"""
    return series.replace([np.inf, -np.inf], np.nan)


__all__ = [
    "MAX_PIVOT_COLUMNS",
    "numeric",
    "as_cols",
    "existing_cols",
    "require_columns",
    "aggregate_func",
    "pct_values",
    "finite",
]

# -*- coding: utf-8 -*-
"""内置算子集合：导入本包即触发全部算子注册。

每个模块按算子族拆分，新增算子只需新增一个模块并在下方追加导入。
"""
from __future__ import annotations

from ops.builtin import (  # noqa: F401
    row_ops,
    col_ops,
    value_ops,
    numeric_ops,
    datetime_ops,
    agg_ops,
    pct_ops,
    reshape_ops,
    combine_ops,
    filter_ops,
    clean_ops,
    quality_ops,
    time_ops,
    analysis_ops,
    rate_ops,
)

__all__ = [
    "row_ops",
    "col_ops",
    "value_ops",
    "numeric_ops",
    "datetime_ops",
    "agg_ops",
    "pct_ops",
    "reshape_ops",
    "combine_ops",
    "filter_ops",
    "clean_ops",
    "quality_ops",
    "time_ops",
    "analysis_ops",
    "rate_ops",
]

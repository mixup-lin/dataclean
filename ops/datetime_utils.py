# -*- coding: utf-8 -*-
"""日期列统一解析：兼容 pandas 2/3 与「带时区 / 不带时区」混合列。"""
from __future__ import annotations

from typing import Any

import pandas as pd


def to_datetime_series(series: pd.Series) -> pd.Series:
    """把混合时区/混合格式的日期列解析为可安全取 ``.dt`` 的序列。

    pandas 3.x 对 ``format="mixed"`` 且同时存在 tz-aware 与 naive 字符串时，
    会把 naive 的值解析成 NaT 或整列退化为 object dtype，导致按年/月分桶漏行。
    向量化解析出现缺失或 object dtype 时，逐元素把 tz-aware 时间戳还原为本地
    墙上时间（不做 UTC 偏移），再统一成 datetime64，避免跨年错位。
    """
    try:
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    except (TypeError, ValueError):  # pragma: no cover - 旧版 pandas 兜底
        parsed = pd.to_datetime(series, errors="coerce")
    if not parsed.isna().any() and parsed.dtype != object:
        return parsed

    def one(value: Any) -> Any:
        if value is None or value is pd.NaT:
            return pd.NaT
        try:
            ts = pd.to_datetime(value, errors="coerce")
        except (TypeError, ValueError):
            return pd.NaT
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_localize(None)   # 保留本地日期，避免 UTC 换算跨年
        return ts

    repaired = pd.to_datetime(
        pd.Series([one(v) for v in series], index=series.index), errors="coerce"
    )
    # 只有逐元素解析确实挽回更多有效日期时才替换，避免破坏原本的 dtype。
    if int(repaired.notna().sum()) >= int(parsed.notna().sum()):
        return repaired
    return parsed


__all__ = ["to_datetime_series"]

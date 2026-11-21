# -*- coding: utf-8 -*-
"""时间序列算子：date_parse / resample / rolling / lag。

补上「有展示层、无计算算子」的缺口：options 里的 resample_freq 只能走旧
transform_data，不能作为 step 组合；窗口、平移、多格式日期解析此前完全缺失。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd

from ops.constants import (
    RESAMPLE_FUNCS, ROLLING_FUNCS, as_list,
    normalize_freq_alias as normalize_freq,
)
from ops.errors import RuleValidationError
from ops.primitives import as_cols, finite
from ops.registry import OpSpec, ParamSpec, register_op


def _to_datetime(series: pd.Series, formats: List[str]) -> pd.Series:
    combined = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in formats:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        combined = combined.fillna(parsed)
    if formats:
        rest = combined.isna()
        if rest.any():
            try:
                fallback = pd.to_datetime(series[rest], format="mixed", errors="coerce")
            except (TypeError, ValueError):
                fallback = pd.to_datetime(series[rest], errors="coerce")
            combined.loc[rest] = fallback
    else:
        try:
            combined = pd.to_datetime(series, format="mixed", errors="coerce")
        except (TypeError, ValueError):
            combined = pd.to_datetime(series, errors="coerce")
    return combined


def apply_date_parse(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        raise RuleValidationError(["date_parse 缺少 columns"])
    for col in cols:
        if col not in df.columns:
            raise RuleValidationError([f"date_parse 列不存在：{col}"])
    formats = [str(f) for f in as_list(step.get("formats")) if str(f)]
    tz = step.get("timezone")
    min_date = step.get("min_date")
    max_date = step.get("max_date")
    frame = df.copy()
    total_bad = 0
    for col in cols:
        parsed = _to_datetime(frame[col], formats)
        if min_date:
            parsed = parsed.where(parsed >= pd.Timestamp(str(min_date)))
        if max_date:
            parsed = parsed.where(parsed <= pd.Timestamp(str(max_date)))
        if tz:
            parsed = parsed.dt.tz_localize(str(tz)) if parsed.dt.tz is None else parsed.dt.tz_convert(str(tz))
        total_bad += int(parsed.isna().sum())
        target = str(step.get("into") or col) if len(cols) == 1 else col
        frame[target] = parsed
    return frame, f"日期解析 {','.join(cols)}（{len(formats)} 个格式，{total_bad} 个无法解析/越界）"


def _date_parse_outputs(step: Dict[str, Any]) -> List[str]:
    into = step.get("into")
    return [str(into)] if isinstance(into, str) and into else []


def _date_parse_refs(step: Dict[str, Any]) -> List[str]:
    return as_cols(step.get("columns") or step.get("column"))


def _validate_date_parse(step, known, tag, errors) -> None:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        errors.append(f"{tag}.columns 不能为空")
    for col in cols:
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    if step.get("formats") is not None and not isinstance(step["formats"], list):
        errors.append(f"{tag}.formats 必须是数组")
    if step.get("window") is not None:
        errors.append(f"{tag} 不支持 window 参数")


def apply_resample(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    time_col = str(step.get("column") or step.get("time_column") or "")
    if time_col not in df.columns:
        raise RuleValidationError([f"resample 时间列不存在：{time_col}"])
    try:
        index = pd.to_datetime(df[time_col], format="mixed", errors="coerce")
    except (TypeError, ValueError):
        index = pd.to_datetime(df[time_col], errors="coerce")
    valid = index.notna()
    if not valid.any():
        raise RuleValidationError(["resample 时间列无可解析日期"])
    funcs = [str(f) for f in as_list(step.get("funcs")) if str(f) in RESAMPLE_FUNCS] or ["mean"]
    value_cols = [c for c in as_cols(step.get("columns")) if c in df.columns]
    numeric_cols = [c for c in df.select_dtypes(include="number").columns if c != time_col]
    values = value_cols or numeric_cols
    if not values:
        raise RuleValidationError(["resample 缺少可聚合的数值列"])
    freq = normalize_freq(str(step.get("freq") or "ME"))
    ts = df.loc[valid, values].set_index(index[valid])
    try:
        out = ts.resample(freq).agg(funcs)
    except ValueError as exc:
        raise RuleValidationError([f"resample 频率非法 {freq!r}：{exc}"]) from exc
    out.columns = [
        "_".join([str(x) for x in col]) if isinstance(col, tuple) else str(col)
        for col in out.columns
    ]
    out = out.reset_index()
    out = out.rename(columns={out.columns[0]: f"{time_col}_{freq}"})
    if step.get("fillna") is not None:
        out = out.fillna(float(step["fillna"]))
    return out, f"时间重采样 {time_col} · {freq}（{funcs}）→ {len(out)} 个周期"


def _resample_outputs(step: Dict[str, Any]) -> List[str]:
    freq = normalize_freq(str(step.get("freq") or "ME"))
    time_col = str(step.get("column") or step.get("time_column") or "")
    funcs = [str(f) for f in as_list(step.get("funcs")) if str(f) in RESAMPLE_FUNCS] or ["mean"]
    values = as_cols(step.get("columns"))
    out = [f"{time_col}_{freq}"]
    for col in values:
        for func in funcs:
            out.append(f"{col}_{func}")
    return out


def _validate_resample(step, known, tag, errors) -> None:
    col = step.get("column") or step.get("time_column")
    if not isinstance(col, str) or not col:
        errors.append(f"{tag}.column 必须是时间列名")
    elif known and col not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {col!r}")
    if step.get("freq") is not None and not isinstance(step["freq"], str):
        errors.append(f"{tag}.freq 必须是字符串")
    funcs = step.get("funcs")
    if funcs is not None:
        bad = [f for f in as_list(funcs) if f not in RESAMPLE_FUNCS]
        if bad:
            errors.append(f"{tag}.funcs 含非法项 {bad}，可选 {list(RESAMPLE_FUNCS)}")


def apply_rolling(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    col = str(step.get("column", ""))
    if col not in df.columns:
        raise RuleValidationError([f"rolling 列不存在：{col}"])
    window = int(step.get("window") or 3)
    if window < 1:
        raise RuleValidationError(["rolling.window 必须是正整数"])
    func = str(step.get("func") or "mean")
    if func not in ROLLING_FUNCS:
        raise RuleValidationError([f"rolling.func 非法：{func}"])
    into = str(step.get("into") or f"{col}_{window}期{func}")
    min_periods = int(step.get("min_periods") or 1)
    series = pd.to_numeric(df[col], errors="coerce")
    group = step.get("by")
    if group and group in df.columns:
        rolled = series.groupby(df[group]).transform(
            lambda s: getattr(s.rolling(window, min_periods=min_periods), func)()
        )
    else:
        rolled = getattr(series.rolling(window, min_periods=min_periods), func)()
    frame = df.copy()
    frame[into] = finite(rolled).round(4)
    return frame, f"滚动窗口 {col}（{window} 期 {func}）→ {into}"


def _rolling_outputs(step: Dict[str, Any]) -> List[str]:
    col = str(step.get("column", ""))
    window = int(step.get("window") or 3)
    func = str(step.get("func") or "mean")
    return [str(step.get("into") or f"{col}_{window}期{func}")]


def _validate_rolling(step, known, tag, errors) -> None:
    col = step.get("column")
    if not isinstance(col, str) or not col:
        errors.append(f"{tag}.column 必须是列名")
    elif known and col not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {col!r}")
    w = step.get("window")
    if w is not None and (not isinstance(w, int) or isinstance(w, bool) or w < 1):
        errors.append(f"{tag}.window 必须是正整数")
    if step.get("func") is not None and step["func"] not in ROLLING_FUNCS:
        errors.append(f"{tag}.func 非法，可选 {list(ROLLING_FUNCS)}")
    group = step.get("by")
    if isinstance(group, str) and known and group not in known:
        errors.append(f"{tag}.by 引用了不存在的列 {group!r}")


def apply_lag(
    df: pd.DataFrame, step: Dict[str, Any], frames=None, ctx=None
) -> Tuple[pd.DataFrame, str]:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        raise RuleValidationError(["lag 缺少 columns"])
    for col in cols:
        if col not in df.columns:
            raise RuleValidationError([f"lag 列不存在：{col}"])
    periods = int(step.get("periods") or 1)
    suffix = str(step.get("suffix") or f"_lag{periods}")
    group = step.get("by")
    frame = df.copy()
    for col in cols:
        shifted = (frame[col].groupby(frame[group]).shift(periods)
                   if group and group in frame.columns else frame[col].shift(periods))
        frame[f"{col}{suffix}"] = shifted
    return frame, f"平移 {','.join(cols)}（{periods} 期）→ 后缀 {suffix}"


def _lag_outputs(step: Dict[str, Any]) -> List[str]:
    periods = int(step.get("periods") or 1)
    suffix = str(step.get("suffix") or f"_lag{periods}")
    return [f"{c}{suffix}" for c in as_cols(step.get("columns") or step.get("column"))]


def _validate_lag(step, known, tag, errors) -> None:
    cols = as_cols(step.get("columns") or step.get("column"))
    if not cols:
        errors.append(f"{tag}.columns 不能为空")
    for col in cols:
        if known and col not in known:
            errors.append(f"{tag}.columns 引用了不存在的列 {col!r}")
    p = step.get("periods")
    if p is not None and (not isinstance(p, int) or isinstance(p, bool)):
        errors.append(f"{tag}.periods 必须是整数")


register_op(OpSpec(
    name="date_parse", layer=1, role="transform", execute=apply_date_parse,
    validate=_validate_date_parse, refs=_date_parse_refs, outputs=_date_parse_outputs,
    produces_time_dim=True,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),
            ParamSpec("formats", list)),
    summary="多格式日期解析（显式格式列表 / 时区 / 有效范围）",
    prompt_example={"op": "date_parse", "columns": ["<日期列>"], "formats": ["%Y/%m/%d", "%Y年%m月%d日"]},
))
register_op(OpSpec(
    name="resample", layer=2, role="analysis", execute=apply_resample,
    validate=_validate_resample, refs=lambda s: [s.get("column") or s.get("time_column")],
    outputs=_resample_outputs, produces_time_dim=True, task_hint="trend",
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("freq", str, required=True)),
    summary="时间序列重采样（日/周/月/季/年，可多聚合函数；兼容 M→ME）",
    prompt_example={"op": "resample", "column": "<日期列>", "freq": "M", "funcs": ["mean", "count"]},
))
register_op(OpSpec(
    name="rolling", layer=1, role="transform", execute=apply_rolling,
    validate=_validate_rolling, refs=lambda s: [s.get("column")],
    outputs=_rolling_outputs,
    params=(ParamSpec("column", str, required=True, column=True),
            ParamSpec("window", int, required=True),
            ParamSpec("func", str, enum=ROLLING_FUNCS)),
    summary="滚动窗口统计（均值/求和/极值，可按组分区）",
    prompt_example={"op": "rolling", "column": "<数值列>", "window": 7, "func": "mean"},
))
register_op(OpSpec(
    name="lag", layer=1, role="transform", execute=apply_lag,
    validate=_validate_lag, refs=_date_parse_refs, outputs=_lag_outputs,
    params=(ParamSpec("columns", list, required=True, column=True, multi=True),
            ParamSpec("periods", int)),
    summary="列平移（环比/同比的滞后项，可按组分区）",
    prompt_example={"op": "lag", "columns": ["<数值列>"], "periods": 1, "by": "<分组列>"},
))

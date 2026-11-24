#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据处理自动化脚本 — Data Processing Automation Pipeline
========================================================
零额外前端依赖（仅 Python 标准库 + pandas + openpyxl）的 ETL 数据管道。

能力矩阵
--------
1. 多数据源读取 : CSV / JSON / Excel(.xlsx) / SQLite
2. 数据清洗管道 : 去重 / 缺失值处理 / 自动类型转换 / 异常值检测(IQR·Z-Score)与处理
3. 转换与聚合   : 分组统计 / 透视表 / 时间序列重采样
4. HTML 质量报告: 统计摘要 + 纯 SVG 数据分布图表（无需 JS / 绘图库）
5. 命令行配置   : argparse 全参数化
6. 日志与错误处理: 分级日志 + 结构化异常捕获
7. 配置模型     : dataclasses 定义，含参数自校验

依赖安装
--------
    pip install pandas openpyxl

运行示例
--------
    python etl_pipeline.py data.csv -f csv --group-by category
    python etl_pipeline.py sales.xlsx -f excel --fill mean --time-col date --resample M
    python etl_pipeline.py app.db -f sqlite --query "SELECT * FROM users" --outlier zscore
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# openpyxl 用于读取 .xlsx；显式导入以便给出友好报错
try:
    import openpyxl  # noqa: F401
except ImportError:  # pragma: no cover
    openpyxl = None


LOGGER = logging.getLogger("etl")


# ============================================================
# 1. 配置模型（dataclasses）
# ============================================================
@dataclass
class PipelineConfig:
    """ETL 管道配置模型：所有运行参数集中管理，并支持启动前自校验。"""

    input_path: str
    input_format: str
    output_dir: str = "./output"
    sqlite_table: str = "data"
    sqlite_query: Optional[str] = None
    excel_sheet: Optional[str] = None
    drop_duplicates: bool = True
    fill_missing: str = "median"
    outlier_method: str = "iqr"          # iqr | zscore | none
    outlier_action: str = "report"       # report | remove | clip
    outlier_threshold: float = 1.5
    group_by: Optional[str] = None
    agg_funcs: Sequence[str] = ("count", "mean", "sum", "min", "max")
    pivot_index: Optional[str] = None
    pivot_columns: Optional[str] = None
    pivot_values: Optional[str] = None
    pivot_agg: str = "mean"
    time_column: Optional[str] = None
    resample_freq: Optional[str] = None  # D | W | M | Q | Y | H ...
    resample_agg: Sequence[str] = ("mean", "sum", "count")
    encoding: str = "utf-8"
    log_level: str = "INFO"
    log_file: Optional[str] = None
    max_charts: int = 6
    # 扩展清洗能力
    normalize_columns: bool = True   # 列名去空格 / 折叠空白 / 去重名
    clean_text: bool = True          # 文本列去空格、折叠空白、空串转缺失
    drop_empty_cols: bool = True     # 剔除全部为缺失的列
    drop_constant_cols: bool = False # 剔除零方差（常量）列，默认关闭以免误删 ID

    _FILL_METHODS = ("none", "median", "mean", "mode", "drop", "zero", "ffill", "bfill")
    _OUTLIER_METHODS = ("iqr", "zscore", "none")
    _OUTLIER_ACTIONS = ("report", "remove", "clip")
    _FORMATS = ("csv", "json", "excel", "sqlite")

    def __post_init__(self) -> None:
        if self.input_format not in self._FORMATS:
            raise ValueError(
                f"不支持的输入格式: {self.input_format!r}，可选: {self._FORMATS}"
            )
        if self.fill_missing not in self._FILL_METHODS:
            raise ValueError(f"fill_missing 非法: {self.fill_missing!r}")
        if self.outlier_method not in self._OUTLIER_METHODS:
            raise ValueError(f"outlier_method 非法: {self.outlier_method!r}")
        if self.outlier_action not in self._OUTLIER_ACTIONS:
            raise ValueError(f"outlier_action 非法: {self.outlier_action!r}")
        if self.outlier_threshold <= 0:
            raise ValueError("outlier_threshold 必须为正数")
        if self.max_charts < 0:
            raise ValueError("max_charts 不能为负数")
        # 透视表三要素一致性校验
        if any([self.pivot_index, self.pivot_columns, self.pivot_values]) and not (
            self.pivot_index and self.pivot_columns and self.pivot_values
        ):
            raise ValueError(
                "透视表需同时提供 --pivot-index / --pivot-columns / --pivot-values"
            )


# ============================================================
# 2. 日志
# ============================================================
def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """配置分级日志：同时输出到控制台与（可选）文件。"""
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


# ============================================================
# 3. 多数据源读取
# ============================================================
def read_data(config: PipelineConfig) -> pd.DataFrame:
    """依配置的输入格式分派到对应读取器，并统一捕获常见异常。"""
    LOGGER.info("开始读取数据源: %s (格式=%s)", config.input_path, config.input_format)
    readers = {
        "csv": _read_csv,
        "json": _read_json,
        "excel": _read_excel,
        "sqlite": _read_sqlite,
    }
    reader = readers[config.input_format]
    try:
        df = reader(config)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"输入文件不存在: {config.input_path}") from exc
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 读取失败: {exc}") from exc
    except (pd.errors.ParserError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"数据解析失败: {exc}") from exc

    if df.empty:
        LOGGER.warning("读取到的数据为空！")
    LOGGER.info("读取完成: %d 行 × %d 列", len(df), df.shape[1])
    return df


def _read_csv(config: PipelineConfig) -> pd.DataFrame:
    return pd.read_csv(config.input_path, encoding=config.encoding)


def _read_json(config: PipelineConfig) -> pd.DataFrame:
    # 先尝试标准 JSON；失败再尝试 JSON Lines
    try:
        return pd.read_json(config.input_path, encoding=config.encoding)
    except ValueError:
        return pd.read_json(config.input_path, lines=True, encoding=config.encoding)


def _read_excel(config: PipelineConfig) -> pd.DataFrame:
    if openpyxl is None:
        raise RuntimeError("未安装 openpyxl，请执行: pip install openpyxl")
    return pd.read_excel(
        config.input_path,
        sheet_name=config.excel_sheet if config.excel_sheet else 0,
        engine="openpyxl",
    )


def _read_sqlite(config: PipelineConfig) -> pd.DataFrame:
    query = config.sqlite_query or f"SELECT * FROM {config.sqlite_table}"
    with sqlite3.connect(config.input_path) as conn:
        return pd.read_sql_query(query, conn)


# ============================================================
# 4. 数据清洗管道
# ============================================================
def clean_data(
    df: pd.DataFrame, config: PipelineConfig
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """执行完整清洗流程，返回 (清洗后数据, 清洗报告元数据)。"""
    original_rows = len(df)
    report: Dict[str, Any] = {"original_rows": original_rows, "dropped_columns": []}

    # 4.0 列名规范化（去空格 / 折叠空白 / 去重名），保证后续引用稳定
    if config.normalize_columns:
        df = _normalize_columns(df)

    # 4.1 原始逐列缺失基线（清洗前统计，用于报告“各列缺失值”）
    report["missing_by_col"] = df.isnull().sum()

    # 4.2 去重
    if config.drop_duplicates:
        before = len(df)
        df = df.drop_duplicates()
        report["duplicates_removed"] = before - len(df)
        LOGGER.info("去重: 移除 %d 行重复记录", report["duplicates_removed"])

    # 4.3 剔除全部为缺失的列
    if config.drop_empty_cols:
        empty_cols = [c for c in df.columns if df[c].isnull().all()]
        if empty_cols:
            df = df.drop(columns=empty_cols)
            report["dropped_columns"].extend(empty_cols)
            LOGGER.info("剔除全空列: %s", ", ".join(map(str, empty_cols)))

    # 4.4 文本清洗（去空格、折叠空白、空串转缺失）
    if config.clean_text:
        df = _clean_text(df)

    # 4.5 缺失值处理
    missing_before = int(df.isnull().sum().sum())
    df = _handle_missing(df, config.fill_missing)
    missing_after = int(df.isnull().sum().sum())
    report["missing_before"] = missing_before
    report["missing_after"] = missing_after
    LOGGER.info(
        "缺失值处理(%s): %d → %d", config.fill_missing, missing_before, missing_after
    )

    # 4.6 自动类型转换
    df, converted = _auto_convert_types(df)
    report["type_conversions"] = converted
    if converted:
        LOGGER.info(
            "类型转换: %s", ", ".join(f"{k}→{v}" for k, v in converted.items())
        )

    # 4.7 剔除常数列（零方差），避免相关性全 NaN 等无意义列
    if config.drop_constant_cols:
        const_cols = [c for c in df.columns if df[c].nunique(dropna=True) <= 1]
        if const_cols:
            df = df.drop(columns=const_cols)
            report["dropped_columns"].extend(const_cols)
            LOGGER.info("剔除常数列: %s", ", ".join(map(str, const_cols)))

    # 4.8 异常值检测与处理
    if config.outlier_method != "none":
        df, n_outliers, bounds = _detect_outliers(
            df, config.outlier_method, config.outlier_threshold, config.outlier_action
        )
        report["outliers"] = {
            "count": int(n_outliers),
            "bounds": bounds,
            "action": config.outlier_action,
        }
        LOGGER.info(
            "异常值(%s / 阈值 %.2f): 检测到 %d 个，动作=%s",
            config.outlier_method,
            config.outlier_threshold,
            n_outliers,
            config.outlier_action,
        )
    else:
        report["outliers"] = {"count": 0, "bounds": {}, "action": "none"}

    report["final_rows"] = len(df)
    LOGGER.info("清洗完成: %d → %d 行", original_rows, len(df))
    return df, report


def _handle_missing(df: pd.DataFrame, method: str) -> pd.DataFrame:
    """缺失值处理。注意：绝不在数值列中写入文本占位符，避免 dtype 污染。"""
    numeric = df.select_dtypes(include="number").columns

    if method == "none":
        # 明细/清洗需求要求「其他字段空值置为 null」时，全局填充必须关闭，
        # 否则后续显式 fill 步骤拿到的已经不是原始空值，填充口径会静默失真。
        return df
    if method == "drop":
        return df.dropna()
    if method == "ffill":
        return df.ffill()
    if method == "bfill":
        return df.bfill()
    if method == "zero":
        df = df.copy()
        df[numeric] = df[numeric].fillna(0)
    elif method in ("mean", "median"):
        df = df.copy()
        for c in numeric:
            m = df[c].mean() if method == "mean" else df[c].median()
            if pd.notna(m):  # 整列全为 NaN 时均值/中位数亦为 NaN，跳过以免污染
                df[c] = df[c].fillna(m)
    elif method == "mode":
        df = df.copy()
        for col in df.columns:
            mode_vals = df[col].mode(dropna=True)
            if not mode_vals.empty:
                df[col] = df[col].fillna(mode_vals.iloc[0])

    # 残余缺失：仅对文本列填充占位符，绝不触碰数值 / 日期 / 布尔列
    obj = df.select_dtypes(include=["object", "string"]).columns
    if len(obj):
        df = df.copy()
        df[obj] = df[obj].fillna("Unknown")
    return df


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """列名规范化：折叠空白、去首尾空格，并对重名列追加序号。"""
    df = df.copy()
    seen: Dict[str, int] = {}
    new_cols = []
    for c in df.columns:
        name = re.sub(r"\s+", " ", str(c)).strip()
        if name in seen:
            seen[name] += 1
            new_cols.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 0
            new_cols.append(name)
    df.columns = new_cols
    return df


def _clean_text(df: pd.DataFrame) -> pd.DataFrame:
    """文本列清洗：去首尾空格、折叠内部空白，并将空串/伪缺失统一为 NaN。"""
    df = df.copy()
    for c in df.select_dtypes(include=["object", "string"]).columns:
        s = df[c].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
        s = s.replace(
            {"": np.nan, "nan": np.nan, "NaN": np.nan, "None": np.nan, "null": np.nan}
        )
        df[c] = s
    return df


def _auto_convert_types(
    df: pd.DataFrame
) -> Tuple[pd.DataFrame, Dict[str, str]]:
    converted: Dict[str, str] = {}
    for col in df.columns:
        if df[col].dtype != object:
            continue
        if df[col].dropna().empty:
            continue
        # 优先尝试日期
        try:
            parsed = pd.to_datetime(df[col], errors="raise")
            df[col] = parsed
            converted[col] = "datetime"
            continue
        except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime):
            pass
        # 再尝试数值
        try:
            parsed = pd.to_numeric(df[col], errors="raise")
            df[col] = parsed
            converted[col] = "numeric"
        except (ValueError, TypeError):
            pass
    return df, converted


def _detect_outliers(
    df: pd.DataFrame,
    method: str,
    threshold: float,
    action: str,
) -> Tuple[pd.DataFrame, int, Dict[str, Tuple[float, float]]]:
    numeric = df.select_dtypes(include="number").columns
    total = 0
    bounds: Dict[str, Tuple[float, float]] = {}
    mask = pd.Series(False, index=df.index)

    for col in numeric:
        s = df[col]
        if method == "iqr":
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            lo, hi = q1 - threshold * iqr, q3 + threshold * iqr
        elif method == "zscore":
            mean = s.mean()
            std = s.std()
            std_use = 1.0 if (std is None or std != std or std == 0) else std
            lo, hi = mean - threshold * std_use, mean + threshold * std_use
        else:
            continue

        bounds[col] = (float(lo), float(hi))
        cond = (s < lo) | (s > hi)
        total += int(cond.sum())

        if action == "remove":
            mask |= cond
        elif action == "clip":
            df[col] = s.clip(lo, hi)

    if action == "remove" and mask.any():
        df = df[~mask]
    return df, total, bounds


# ============================================================
# 5. 数据转换与聚合
# ============================================================
# pandas 3.x 变更了部分重采样别名（如 M→ME, Y→YE, Q→QE），兼容映射的唯一事实源
# 在 ops.constants，避免核心管道与算子层各维护一份而漂移。
from ops.constants import FREQ_ALIASES as _FREQ_ALIASES  # noqa: E402


def _normalize_freq(freq: Optional[str]) -> Optional[str]:
    if not freq:
        return freq
    return _FREQ_ALIASES.get(freq.upper(), freq)


def transform_data(df: pd.DataFrame, config: PipelineConfig) -> Dict[str, Any]:
    """分组统计 / 透视表 / 时间序列重采样。"""
    # 空数据 / 无列数据：pandas 的 describe() 会抛
    # "Cannot describe a DataFrame without columns"，此处兜底为空摘要，
    # 保证后续报告与导出链路不会因为一份空数据源而整体失败。
    if df.empty or df.shape[1] == 0:
        results: Dict[str, Any] = {"summary": pd.DataFrame()}
    else:
        results = {"summary": df.describe(include="all")}

    # 5.1 分组统计（数值列应用全部聚合函数，非数值列仅计数）
    if config.group_by and config.group_by in df.columns:
        numeric = df.select_dtypes(include="number").columns
        agg_map = {c: list(config.agg_funcs) for c in numeric}
        for c in df.columns:
            if c != config.group_by and c not in numeric:
                agg_map[c] = "count"
        grouped = df.groupby(config.group_by).agg(agg_map)
        results["grouped"] = grouped
        LOGGER.info("分组统计(%s): %d 个分组", config.group_by, grouped.shape[0])

    # 5.2 透视表
    if config.pivot_index and config.pivot_columns and config.pivot_values:
        pivot = pd.pivot_table(
            df,
            index=config.pivot_index,
            columns=config.pivot_columns,
            values=config.pivot_values,
            aggfunc=config.pivot_agg,
        )
        results["pivot"] = pivot
        LOGGER.info("透视表: %d 行 × %d 列", pivot.shape[0], pivot.shape[1])

    # 5.3 时间序列重采样
    if config.time_column and config.resample_freq:
        freq = _normalize_freq(config.resample_freq)
        if config.time_column not in df.columns:
            LOGGER.warning("时间列 %r 不存在，跳过重采样", config.time_column)
        else:
            ts = df.copy()
            ts[config.time_column] = pd.to_datetime(ts[config.time_column], errors="coerce")
            ts = ts.dropna(subset=[config.time_column]).set_index(config.time_column)
            num = ts.select_dtypes(include="number").columns
            if len(num):
                resampled = ts[num].resample(freq).agg(list(config.resample_agg))
                results["resampled"] = resampled
                LOGGER.info("重采样(%s): %d 个周期", freq, resampled.shape[0])
            else:
                LOGGER.warning("重采样时无数值列可聚合")

    return results


# ============================================================
# 6. 纯 SVG 数据分布图表（无第三方绘图库）
# ============================================================
# 展示层格式化工具已从本模块抽到中性的 formatting.py，以便 ops 算子层复用，
# 切断 ops.builtin → etl_pipeline 的反向依赖。此处重导出保持历史导入路径可用。
from formatting import _esc, _fmt_num, _next_svg_id  # noqa: E402


def _trunc(s: Any, n: int = 14) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _svg_bar(
    items: List[Tuple[Any, float]],
    title: str,
    color: str = "#2563eb",
    width: int = 460,
    height: int = 290,
    max_bars: int = 12,
    y_fmt: Optional[Any] = None,
) -> str:
    """通用柱状图：含网格线、Y 轴刻度、零基线、数值标注与倾斜标签。"""
    items = items[:max_bars]
    if not items:
        return ""
    labels = [str(l) for l, _ in items]
    values = [float(v) for _, v in items]
    vmax, vmin = max(values), min(values)
    if vmax <= 0:
        vmax = 1.0
    lo = min(vmin, 0.0)
    span = vmax - lo or 1.0
    pad_l, pad_r, pad_t, pad_b = 58, 16, 36, 70
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(items)
    gap = 8
    bar_w = (plot_w - gap * (n - 1)) / max(n, 1)
    gid = _next_svg_id("bar")
    fg = y_fmt or _fmt_num

    def y_of(v: float) -> float:
        return pad_t + plot_h * (1 - (v - lo) / span)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{_esc(title)}">',
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.95"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0.55"/></linearGradient></defs>',
        f'<text x="10" y="22" font-size="13.5" font-family="system-ui" '
        f'font-weight="600" fill="#111827">{_esc(title)}</text>',
    ]
    for t in range(5):
        gy = pad_t + plot_h * t / 4
        val = vmax - span * t / 4
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{pad_l + plot_w}" y2="{gy:.1f}" '
            f'stroke="#eef2f7" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{gy + 3:.1f}" font-size="8.5" '
            f'text-anchor="end" fill="#94a3b8">{fg(val)}</text>'
        )
    base = y_of(0.0 if lo <= 0 <= vmax else lo)
    parts.append(
        f'<line x1="{pad_l}" y1="{base:.1f}" x2="{pad_l + plot_w}" y2="{base:.1f}" '
        f'stroke="#cbd5e1" stroke-width="1"/>'
    )
    for i, (lab, val) in enumerate(zip(labels, values)):
        x = pad_l + i * (bar_w + gap)
        yt = y_of(val)
        top = min(base, yt)
        h = max(abs(base - yt), 0.5)
        parts.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="url(#{gid})" rx="3"/>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{yt - 4:.1f}" font-size="9.5" '
            f'text-anchor="middle" fill="#334155" font-weight="600">{fg(val)}</text>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{base + 14:.1f}" font-size="9.5" '
            f'text-anchor="end" '
            f'transform="rotate(-42 {x + bar_w / 2:.1f} {base + 14:.1f})" '
            f'fill="#64748b">{_esc(_trunc(lab))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_histogram(
    series: pd.Series,
    title: str,
    bins: int = 12,
    color: str = "#0ea5e9",
    width: int = 460,
    height: int = 290,
) -> str:
    series = series.dropna()
    if series.empty:
        return ""
    edges = pd.cut(series, bins=bins, include_lowest=True).value_counts().sort_index()
    items = [
        (f"{_fmt_num(iv.left)} ~ {_fmt_num(iv.right)}", int(cnt))
        for iv, cnt in edges.items()
    ]
    return _svg_bar(items, title, color=color, width=width, height=height, max_bars=bins)


def _svg_heatmap(corr: pd.DataFrame, title: str, width: int = 500, height: int = 470) -> str:
    """相关性热力图：发散配色 + 自适应文字颜色 + 色阶图例。"""
    cols = list(corr.columns)
    n = len(cols)
    if n == 0:
        return ""
    cell = 42
    pad_l, pad_t = 116, 46
    size = n * cell
    legend_h = 18
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {pad_l + size + 10} {pad_t + size + 40 + legend_h}" '
        f'width="{pad_l + size + 10}" height="{pad_t + size + 40 + legend_h}" '
        f'role="img" aria-label="{_esc(title)}">',
        f'<text x="10" y="24" font-size="13.5" font-family="system-ui" '
        f'font-weight="600" fill="#111827">{_esc(title)}</text>',
    ]

    def color_of(v: float) -> Tuple[int, int, int]:
        if v >= 0:
            return 255, int(255 - 140 * v), int(255 - 205 * v)
        return int(255 + 175 * v), int(255 + 175 * v), 255

    # 单元格
    for i, ci in enumerate(cols):
        for j, cj in enumerate(cols):
            v = corr.iloc[i, j]
            if pd.isna(v):
                r, g, b, label = 226, 232, 240, "—"
            else:
                vf = float(v)
                r, g, b = color_of(vf)
                label = f"{vf:.2f}"
            x = pad_l + j * cell
            y = pad_t + i * cell
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            tcol = "#ffffff" if lum < 150 else "#1f2937"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell - 2}" height="{cell - 2}" '
                f'rx="3" fill="rgb({r},{g},{b})"/>'
            )
            parts.append(
                f'<text x="{x + cell / 2:.0f}" y="{y + cell / 2 + 3:.0f}" '
                f'font-size="9.5" text-anchor="middle" fill="{tcol}">{label}</text>'
            )
    # 行列标签
    for i, c in enumerate(cols):
        parts.append(
            f'<text x="{pad_l + i * cell + cell / 2:.0f}" y="{pad_t + size + 14:.0f}" '
            f'font-size="9.5" text-anchor="middle" fill="#64748b">{_esc(_trunc(c, 9))}</text>'
        )
        parts.append(
            f'<text x="{pad_l - 8:.0f}" y="{pad_t + i * cell + cell / 2 + 3:.0f}" '
            f'font-size="9.5" text-anchor="end" fill="#64748b">{_esc(_trunc(c, 9))}</text>'
        )
    # 色阶图例（蓝 -1 → 白 0 → 红 +1）
    lx, ly = pad_l, pad_t + size + 26
    lw = size
    for k in range(lw):
        frac = k / max(lw - 1, 1)
        v = -1 + 2 * frac
        r, g, b = color_of(v)
        parts.append(
            f'<rect x="{lx + k}" y="{ly}" width="1" height="{legend_h}" '
            f'fill="rgb({r},{g},{b})"/>'
        )
    parts.append(
        f'<text x="{lx}" y="{ly + legend_h + 12:.0f}" font-size="9" '
        f'text-anchor="start" fill="#64748b">-1.00</text>'
    )
    parts.append(
        f'<text x="{lx + lw / 2:.0f}" y="{ly + legend_h + 12:.0f}" font-size="9" '
        f'text-anchor="middle" fill="#64748b">0.00</text>'
    )
    parts.append(
        f'<text x="{lx + lw}" y="{ly + legend_h + 12:.0f}" font-size="9" '
        f'text-anchor="end" fill="#64748b">+1.00</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ============================================================
# 7. HTML 质量报告
# ============================================================
def _section(title: str, table_html: str) -> str:
    return f'<section class="card"><h2>{_esc(title)}</h2>{table_html}</section>'


def _svg_gauge(score: int) -> str:
    """渲染环形质量评分仪表盘（纯 SVG，无依赖）。"""
    score = max(0, min(100, int(score)))
    r = 34
    cx = cy = 42
    circ = 2 * 3.141592653589793 * r
    dash = circ * score / 100
    color = "#16a34a" if score >= 85 else "#f59e0b" if score >= 70 else "#ef4444"
    grade = "优" if score >= 85 else "良" if score >= 70 else "待改善"
    return (
        f'<svg width="84" height="84" viewBox="0 0 84 84" role="img" '
        f'aria-label="数据质量评分 {score}">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="9"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="9" '
        f'stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>'
        f'<text x="42" y="40" font-size="21" font-weight="700" text-anchor="middle" '
        f'fill="{color}">{score}</text>'
        f'<text x="42" y="56" font-size="9" text-anchor="middle" fill="#64748b">{grade}</text>'
        f"</svg>"
    )


def _detect_scenario(df: pd.DataFrame, config: PipelineConfig):
    """依据列特征与管道配置推断业务场景，使报告更具针对性。"""
    cols = {str(c).lower() for c in df.columns}
    sales_keys = {
        "price", "amount", "sales", "revenue", "qty", "quantity",
        "订单", "销售额", "销量", "金额", "单价", "profit", "成本", "cost",
    }
    time_keys = {"date", "time", "datetime", "timestamp", "日期", "时间"}
    high_card = [c for c in df.columns if df[c].nunique(dropna=True) > 50]

    if config.time_column and config.resample_freq:
        return (
            "时间序列趋势分析",
            "检测到时间维度与重采样配置，报告聚焦周期趋势、季节性波动与同比变化。",
            ["时间维度", "周期聚合", "趋势诊断"],
        )
    if config.pivot_index and config.pivot_columns and config.pivot_values:
        return (
            "多维透视分析",
            "检测到透视表配置，报告聚焦多维交叉对比与业务结构拆解。",
            ["多维交叉", "结构拆解", "下钻分析"],
        )
    if config.group_by:
        return (
            "分组对比分析",
            f"按 <code>{_esc(config.group_by)}</code> 分组，报告聚焦分组间的指标差异与分布偏移。",
            ["分组对比", "指标差异", "分布偏移"],
        )
    if cols & sales_keys:
        return (
            "销售 / 财务分析",
            "检测到金额、销量、成本等业务指标，报告聚焦营收质量、客单水平与交易健康度。",
            ["营收指标", "交易质量", "客单分析"],
        )
    if cols & time_keys:
        return (
            "时序业务数据",
            "检测到时间相关字段，建议补充 <code>--time-col / --resample</code> 以解锁趋势分析。",
            ["时间字段", "趋势建议", "周期特征"],
        )
    if high_card:
        return (
            "实体 / 用户画像",
            "检测到高基数特征列，报告聚焦样本构成、分布画像与长尾分布。",
            ["高基数特征", "分布画像", "样本构成"],
        )
    return (
        "通用数据清洗",
        "未识别到特定业务模式，按通用数据质量视角输出基线报告。",
        ["通用视角", "质量基线", "结构概览"],
    )


def _quality_score(df: pd.DataFrame, clean_report: Dict[str, Any]) -> int:
    """综合缺失率、重复率与异常率给出 0-100 质量评分。"""
    n = len(df)
    if n == 0:
        return 0
    ncols = max(df.shape[1], 1)
    miss_rate = int(df.isnull().sum().sum()) / (n * ncols)
    dup_rate = int(df.duplicated().sum()) / n
    num = df.select_dtypes(include="number").columns
    out = clean_report.get("outliers", {}).get("count", 0)
    out_rate = (out / (n * len(num))) if len(num) else 0.0
    penalty = miss_rate * 45 + dup_rate * 30 + min(out_rate, 1.0) * 25
    return max(0, min(100, int(round(100 - penalty))))


def _flatten_df(df: pd.DataFrame) -> pd.DataFrame:
    """展平多级索引列/行，避免 to_html 输出混乱的复合表头。"""
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join(str(x) for x in c if str(x) != "").strip() for c in df.columns
        ]
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index()
    return df


def _styled_table(df: pd.DataFrame, index: bool = True) -> str:
    """输出带数值格式化（禁用科学计数法）与统一样式类的 HTML 表格。"""
    df = _flatten_df(df)
    fmt = {c: _fmt_num for c in df.columns}
    table = df.to_html(index=index, formatters=fmt, classes="data-table", border=0)
    return f'<div class="table-scroll">{table}</div>'


def _select_chart_cols(
    df: pd.DataFrame, numeric_cols: List[str], max_charts: int
) -> List[str]:
    """挑选出适合绘制分布图的数值列：跳过常量列与近似唯一的 ID 类列，
    使真正有信息量（含大额数值）的列优先进入图表，避免被顺序靠前的大宽表列挤掉。"""
    picked: List[str] = []
    n = len(df)
    for c in numeric_cols:
        s = df[c].dropna()
        if s.empty or s.nunique() <= 1:
            continue  # 常量 / 全空
        if s.nunique() > 0.9 * n:
            continue  # 近似唯一（ID / 序号类）
        picked.append(c)
        if len(picked) >= max_charts:
            break
    return picked


def _quality_scenario_key(config: PipelineConfig,
                          transform_results: Dict[str, Any]) -> str:
    """报告章节的路由键：与本次真实分析动作一致，而不是固定套模板。"""
    if config.time_column and config.resample_freq:
        return "trend"
    if "resampled" in transform_results:
        return "trend"
    if (config.pivot_index and config.pivot_columns and config.pivot_values) \
            or "pivot" in transform_results:
        return "pivot"
    if config.group_by or "grouped" in transform_results:
        return "group"
    return "general"


def _quality_scenario_charts(
    df: pd.DataFrame,
    numeric_cols: List[str],
    transform_results: Dict[str, Any],
    config: PipelineConfig,
    scenario_key: str,
) -> Tuple[List[str], str]:
    """返回 (场景图表, 场景专属章节 HTML)；不再无条件生成相关性热力图。"""
    from charts_svg import heatmap as svg_heatmap
    from charts_svg import line as svg_line

    charts: List[str] = []
    section = ""

    if scenario_key == "trend":
        resampled = transform_results.get("resampled")
        if isinstance(resampled, pd.DataFrame) and not resampled.empty:
            x_col = resampled.columns[0]
            value_cols = [c for c in resampled.columns[1:]
                          if pd.api.types.is_numeric_dtype(resampled[c])]
            if value_cols:
                points = [(str(v), float(resampled.iloc[i][value_cols[0]]))
                          for i, v in enumerate(resampled[x_col])]
                svg = svg_line(points, label=f"{x_col} · {value_cols[0]} 趋势")
                if svg:
                    charts.append(svg)
                    section += _section("时间趋势", f'<div class="chart-card">{svg}</div>')
        if config.time_column and config.time_column in df.columns:
            parsed = pd.to_datetime(df[config.time_column], errors="coerce", format="mixed")
            periods = parsed.dt.to_period("M").astype(str)
            counts = periods[periods != "NaT"].value_counts().sort_index()
            if not counts.empty:
                svg = _svg_bar([(str(k), int(v)) for k, v in counts.items()],
                               "每月样本量（数据覆盖度）", color="#0ea5e9")
                charts.append(svg)
                section += _section(
                    "周期覆盖度",
                    f'<div class="chart-card">{svg}</div>'
                    '<p class="meta">按时间列推导的月度样本量，用于判断趋势是否受数据稀疏影响。</p>',
                )
        if not section:
            section = _section(
                "趋势诊断",
                '<p class="meta">本次未产生可绘制的周期序列；请检查时间列是否可解析。</p>',
            )
        return charts, section

    if scenario_key == "pivot":
        table = transform_results.get("pivot")
        if isinstance(table, pd.DataFrame) and not table.empty:
            rows = [str(i) for i in list(table.index)[:14]]
            cols = [str(c) for c in list(table.columns)[:10]]
            matrix = []
            for row in list(table.values)[:14]:
                matrix.append([
                    float(v) if isinstance(v, (int, float)) and np.isfinite(v) else 0.0
                    for v in list(row)[:10]
                ])
            svg = svg_heatmap(rows, cols, matrix, label="交叉表热力分布")
            if svg:
                charts.append(svg)
                section += _section("交叉结构热力", f'<div class="chart-card">{svg}</div>')
        return charts, section

    if scenario_key == "group":
        table = transform_results.get("grouped")
        if isinstance(table, pd.DataFrame) and not table.empty and len(table.columns) >= 2:
            label_col = table.columns[0]
            value_col = next(
                (c for c in table.columns[1:] if pd.api.types.is_numeric_dtype(table[c])),
                None,
            )
            if value_col is not None:
                svg = _svg_bar(
                    [(str(v), float(table.iloc[i][value_col]))
                     for i, v in enumerate(table[label_col])],
                    f"{label_col} · {value_col} 分组对比", color="#8b5cf6",
                )
                charts.append(svg)
                section += _section("分组对比", f'<div class="chart-card">{svg}</div>')
        return charts, section

    # 通用质量视角：相关性热力图只在这种场景出现，且列数足够多才有意义
    if len(numeric_cols) >= 3:
        corr = df[numeric_cols].corr()
        svg = _svg_heatmap(corr, "数值列相关性热力图")
        if svg:
            charts.append(svg)
            section += _section(
                "数值列相关性",
                f'<div class="chart-card" style="overflow-x:auto">{svg}</div>'
                '<p class="meta">仅在通用质量视角下提供；趋势/分组/透视场景不展示该图。</p>',
            )
    return charts, section


def generate_report(
    df: pd.DataFrame,
    clean_report: Dict[str, Any],
    transform_results: Dict[str, Any],
    config: PipelineConfig,
) -> str:
    out = Path(config.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out / f"quality_report_{ts}.html"

    numeric_cols = list(df.select_dtypes(include="number").columns)
    scenario_key = _quality_scenario_key(config, transform_results)

    # 分布图表：按场景挑选。趋势/透视/分组场景使用场景图表，不再固定塞直方图与相关矩阵
    if scenario_key == "general":
        chart_num_cols = _select_chart_cols(df, numeric_cols, config.max_charts)
        charts: List[str] = []
        for col in chart_num_cols:
            charts.append(_svg_histogram(df[col], f"分布直方图 · {col}"))
        cat_cols = [c for c in df.columns if c not in numeric_cols]
        shown = 0
        for col in cat_cols:
            if shown >= max(0, config.max_charts - len(charts)):
                break
            if df[col].nunique(dropna=True) <= 20:
                vc = df[col].value_counts().head(10)
                charts.append(
                    _svg_bar(
                        [(str(k), int(v)) for k, v in vc.items()],
                        f"取值分布 · {col}", color="#f59e0b",
                    )
                )
                shown += 1
        extra, scenario_section = _quality_scenario_charts(
            df, numeric_cols, transform_results, config, scenario_key
        )
        charts.extend(extra)
    else:
        charts, scenario_section = _quality_scenario_charts(
            df, numeric_cols, transform_results, config, scenario_key
        )

    scenario_name, scenario_desc, scenario_tags = _detect_scenario(df, config)
    score = _quality_score(df, clean_report)
    tags_html = "".join(
        f'<span class="tag">{_esc(t)}</span>' for t in scenario_tags
    )

    raw_missing = clean_report.get("missing_by_col", pd.Series(dtype="int64"))
    raw_missing = raw_missing.reindex(df.columns).fillna(0).astype("int64")
    dropped = clean_report.get("dropped_columns", [])

    metrics = [
        ("总行数", len(df)),
        ("总列数", df.shape[1]),
        ("原始缺失", int(raw_missing.sum())),
        ("重复行", clean_report.get("duplicates_removed", 0)),
        ("异常值", clean_report.get("outliers", {}).get("count", 0)),
        ("剔除列", len(dropped)),
    ]
    metric_html = "".join(
        f'<div class="metric"><div class="value">{_fmt_num(v)}</div>'
        f'<div class="label">{_esc(k)}</div></div>'
        for k, v in metrics
    )
    charts_html = "".join(f'<div class="chart-card">{c}</div>' for c in charts if c)
    scenario_chart_title = {
        "trend": "趋势与周期图表",
        "pivot": "交叉结构图表",
        "group": "分组对比图表",
    }.get(scenario_key, "数据分布图表")
    if scenario_key == "general":
        distribution_section = (
            f'<section class="card"><h2>{scenario_chart_title}</h2>'
            f'<div class="charts">{charts_html}</div></section>'
        )
    else:
        distribution_section = ""  # 场景图表由 scenario_section 统一渲染，避免重复

    out_detail = clean_report.get("outliers", {})
    if out_detail.get("bounds"):
        out_rows = "".join(
            f"<tr><td>{_esc(col)}</td><td>{_fmt_num(lo)}</td><td>{_fmt_num(hi)}</td>"
            f"<td>{_esc(out_detail['action'])}</td></tr>"
            for col, (lo, hi) in out_detail["bounds"].items()
        )
    else:
        out_rows = (
            '<tr><td colspan="4" style="text-align:center;color:#94a3b8">'
            "未检测到异常值 / 已禁用</td></tr>"
        )

    # 各列缺失值（清洗前基线）：缺失数量 + 缺失率
    miss_total = max(len(df), 1)
    miss_tbl = pd.DataFrame(
        {
            "缺失数量": raw_missing,
            "缺失率(%)": (raw_missing / miss_total * 100).astype(float),
        }
    )
    missing_html = _styled_table(miss_tbl)

    dropped_note = ""
    if dropped:
        dropped_note = (
            f'<p class="meta">已自动剔除 {len(dropped)} 个低信息列'
            f'（全空 / 常量）：{_esc(", ".join(map(str, dropped)))}</p>'
        )

    extra_tables = ""
    if "result_table" in transform_results:
        extra_tables += _section(
            "处理结果（最多 100 行 / 20 列）",
            _styled_table(transform_results["result_table"], index=False),
        )
    if "grouped" in transform_results:
        grouped = transform_results["grouped"]
        if isinstance(grouped, pd.DataFrame) and len(grouped) > 200:
            grouped = grouped.head(200)
        extra_tables += _section("分组统计（最多 200 行）", _styled_table(grouped))
    if "pivot" in transform_results:
        extra_tables += _section("透视表", _styled_table(transform_results["pivot"]))
    if "resampled" in transform_results:
        extra_tables += _section(
            "时间序列重采样", _styled_table(transform_results["resampled"])
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>数据质量报告</title>
<style>
  :root {{ --brand:#2563eb; --brand2:#0ea5e9; --ink:#0f172a; --muted:#64748b; --line:#e5e7eb; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: var(--ink); background:#f1f5f9; margin:0; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
  .banner {{ display:flex; justify-content:space-between; align-items:center; gap:24px;
    background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 55%, #0ea5e9 100%);
    color:#fff; border-radius:18px; padding:26px 30px;
    box-shadow:0 10px 30px rgba(37,99,235,.25); }}
  .banner h1 {{ margin:0 0 6px; font-size:26px; letter-spacing:.5px; }}
  .banner .meta {{ margin:0 0 14px; font-size:13px; color:#dbeafe; }}
  .scenario-name {{ display:inline-block; font-size:15px; font-weight:700;
    background:rgba(255,255,255,.18); padding:4px 12px; border-radius:999px; }}
  .tags {{ margin-top:10px; display:flex; flex-wrap:wrap; gap:6px; }}
  .tag {{ font-size:12px; background:rgba(255,255,255,.14); padding:3px 10px; border-radius:999px; }}
  .scenario-desc {{ margin:12px 0 0; font-size:13px; line-height:1.6; color:#e0f2fe; max-width:660px; }}
  .gauge {{ flex:0 0 auto; }}
  .metrics {{ display:flex; flex-wrap:wrap; gap:14px; margin:22px 0; }}
  .metric {{ flex:1 1 130px; background:#fff; border:1px solid var(--line); border-radius:14px;
    padding:16px 18px; text-align:center; box-shadow:0 2px 8px rgba(15,23,42,.04); }}
  .metric .value {{ font-size:28px; font-weight:800; color: var(--brand); line-height:1.1; }}
  .metric .label {{ font-size:12px; color: var(--muted); margin-top:6px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:16px; padding:20px 22px;
    margin:18px 0; box-shadow:0 2px 10px rgba(15,23,42,.04); }}
  .card h2 {{ margin:0 0 14px; font-size:18px; padding-bottom:10px; border-bottom:2px solid #eff6ff;
    display:flex; align-items:center; gap:8px; }}
  .card h2::before {{ content:""; width:6px; height:18px; border-radius:3px;
    background:linear-gradient(var(--brand), var(--brand2)); display:inline-block; }}
  .charts {{ display:flex; flex-wrap:wrap; gap:14px; }}
  .chart-card {{ background:#fbfdff; border:1px solid var(--line); border-radius:12px; padding:8px; }}
  table.data-table {{ border-collapse: collapse; width:100%; font-size:13px; background:#fff; }}
  table.data-table th, table.data-table td {{ border:1px solid var(--line); padding:7px 10px; text-align:left; }}
  table.data-table thead th {{ background:#f1f5f9; font-weight:600; color:#334155; }}
  table.data-table tbody tr:nth-child(even) {{ background:#fafcff; }}
  .table-scroll {{ overflow-x:auto; max-width:100%; -webkit-overflow-scrolling:touch; }}
  .meta {{ color: var(--muted); font-size:13px; }}
  code {{ background:#eef2ff; color:#3730a3; padding:2px 6px; border-radius:5px; font-size:12px; }}
  .foot {{ color: var(--muted); font-size:12px; text-align:center; margin:26px 0 8px; }}
</style></head>
<body><div class="wrap">
<header class="banner">
  <div class="banner-main">
    <h1>📊 数据质量报告</h1>
    <p class="meta">生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} ｜ 数据源：{_esc(config.input_path)} ｜ 格式：{_esc(config.input_format)}</p>
    <div class="scenario">
      <span class="scenario-name">{_esc(scenario_name)}</span>
      <div class="tags">{tags_html}</div>
    </div>
    <p class="scenario-desc">{scenario_desc}</p>
  </div>
  <div class="gauge">{_svg_gauge(score)}</div>
</header>

<div class="metrics">{metric_html}</div>

<section class="card"><h2>列类型与结构</h2>
{_styled_table(df.dtypes.to_frame("数据类型"))}
</section>

<section class="card"><h2>统计摘要（按列转置，列多时可横向滚动）</h2>
{_styled_table(transform_results["summary"].T)}
</section>

<section class="card"><h2>各列缺失值（清洗前基线）</h2>
{missing_html}
{dropped_note}
</section>

<section class="card"><h2>异常值检测明细（{_esc(out_detail.get('action', 'none'))}）</h2>
<div class="table-scroll"><table class="data-table"><thead><tr><th>列</th><th>下界</th><th>上界</th><th>动作</th></tr></thead><tbody>{out_rows}</tbody></table></div>
</section>

{distribution_section}

{scenario_section}

{extra_tables}

<p class="foot">由 etl_pipeline.py 自动生成 · 报告路径：{_esc(str(path))}</p>
</div></body></html>"""

    path.write_text(html_doc, encoding="utf-8")
    LOGGER.info("HTML 质量报告已生成: %s", path)
    return str(path)


# ============================================================
# 8. 命令行入口
# ============================================================
def _parse_agg(value: str) -> List[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="etl_pipeline.py",
        description="数据处理自动化脚本：读取 → 清洗 → 转换 → 生成质量报告",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", help="输入文件路径")
    p.add_argument("-f", "--format", required=True,
                   choices=["csv", "json", "excel", "sqlite"], help="输入格式")
    p.add_argument("-o", "--output", default="./output", help="报告输出目录")
    p.add_argument("--table", default="data", help="SQLite 表名（未指定 --query 时）")
    p.add_argument("--query", default=None, help="SQLite 自定义 SQL（覆盖 --table）")
    p.add_argument("--sheet", default=None, help="Excel 工作表名/索引")
    p.add_argument("--no-dedup", action="store_true", help="跳过去重")
    p.add_argument("--fill", default="median",
                   choices=["median", "mean", "mode", "drop", "zero", "ffill", "bfill"],
                   help="缺失值填充策略")
    p.add_argument("--outlier", default="iqr",
                   choices=["iqr", "zscore", "none"], help="异常值检测方法")
    p.add_argument("--outlier-action", default="report",
                   choices=["report", "remove", "clip"], help="异常值处理动作")
    p.add_argument("--outlier-threshold", type=float, default=1.5,
                   help="异常值阈值(系数)")
    p.add_argument("--group-by", default=None, help="分组统计列")
    p.add_argument("--agg", default="count,mean,sum,min,max",
                   help="分组聚合函数(逗号分隔)")
    p.add_argument("--pivot-index", default=None, help="透视表行")
    p.add_argument("--pivot-columns", default=None, help="透视表列")
    p.add_argument("--pivot-values", default=None, help="透视表值")
    p.add_argument("--pivot-agg", default="mean", help="透视表聚合函数")
    p.add_argument("--time-col", default=None, help="时间序列列名")
    p.add_argument("--resample", default=None, help="重采样频率(D/W/M/Q/Y/H...)")
    p.add_argument("--resample-agg", default="mean,sum,count",
                   help="重采样聚合函数")
    p.add_argument("--encoding", default="utf-8", help="文本编码")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    p.add_argument("--log-file", default=None, help="日志文件路径")
    p.add_argument("--max-charts", type=int, default=6, help="报告最多渲染图表数")
    p.add_argument("--no-normalize-cols", action="store_true",
                   help="关闭列名规范化（去空格/折叠空白/去重名）")
    p.add_argument("--no-clean-text", action="store_true",
                   help="关闭文本列清洗（去空格/空串转缺失）")
    p.add_argument("--no-drop-empty", action="store_true",
                   help="关闭自动剔除全空列")
    p.add_argument("--drop-constant", action="store_true",
                   help="开启自动剔除零方差（常量）列")
    p.add_argument("--version", action="version", version="etl_pipeline 1.0.0")
    return p


def run_pipeline(config: PipelineConfig) -> str:
    """可复用的核心执行链：读取 → 清洗 → 转换 → 生成质量报告。

    返回值与 ``generate_report`` 一致（HTML 报告路径字符串）。
    供 CLI 与规则执行层共用，语义与原有 main 执行段完全一致。
    """
    df = read_data(config)
    df, clean_report = clean_data(df, config)
    transform_results = transform_data(df, config)
    return generate_report(df, clean_report, transform_results, config)


# ============================================================
# 9. 扩展子命令（前缀分派，旧调用形式 100% 保持）
# ============================================================
_SUBCOMMANDS = ("nl", "extract", "web", "llm-check", "rules", "cleanup", "showcase")


def _cmd_nl(argv: List[str]) -> int:
    """自然语言 → 规则生成 → 规则执行 → 导出。"""
    import argparse as _ap

    p = _ap.ArgumentParser(
        prog="etl_pipeline.py nl",
        description="用自然语言描述数据处理需求，自动生成规则并执行",
        formatter_class=_ap.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("intent", nargs="+", help="自然语言需求描述")
    p.add_argument("-s", "--source", default="lots.csv", help="数据源路径")
    p.add_argument("-f", "--format", default="csv",
                   choices=["csv", "json", "excel", "sqlite"], help="数据源格式")
    p.add_argument("-o", "--output", default="./output", help="产物输出根目录")
    p.add_argument("--columns", default="", help="可用列名(逗号分隔)，留空自动探测")
    p.add_argument("--rule-file", default="", help="直接执行已有规则 JSON，跳过 LLM")
    p.add_argument("--rule", default="", help="从规则库按名称/ID 载入已保存规则（与 --rule-file 二选一）")
    p.add_argument("--save-rule", default="", metavar="NAME",
                   help="把本次规则保存到规则库，后续可用 --rule NAME 复用")
    p.add_argument("--rules-only", action="store_true", help="只生成并打印规则，不执行")
    p.add_argument("--generation-path", default="schema", choices=["schema", "legacy"],
                   help="生成链路：schema 为默认主路径；legacy 仅供回归诊断")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    a = p.parse_args(argv)
    setup_logging(a.log_level)
    LOGGER.info("自然语言规则链路启动")

    try:
        if a.rule_file and a.rule:
            raise ValueError("--rule-file 与 --rule 只能指定一个")
        if a.rule:
            from rule_store import load_rule_dict

            rule = load_rule_dict(a.rule)
            meta = {"provider": "rule-store", "mode": "offline", "degraded": False}
        elif a.rule_file:
            import json as _json
            from pathlib import Path as _Path

            rule = _json.loads(_Path(a.rule_file).read_text(encoding="utf-8"))
            meta = {"provider": "file", "mode": "offline", "degraded": False}
        else:
            from rules import generate_rules

            intent = " ".join(a.intent)
            source = {"path": a.source, "format": a.format}
            columns = [c.strip() for c in a.columns.split(",") if c.strip()]
            if not columns:
                from rules import probe_columns

                columns = probe_columns(source)
            rule, meta = generate_rules(
                intent, source, columns, generation_path=a.generation_path
            )
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("规则生成失败: %s", exc)
        return 1

    if a.save_rule:
        try:
            from rule_store import save_rule

            saved = save_rule(a.save_rule, rule, intent=" ".join(a.intent))
            print(f"=== 规则已保存：{saved.name} (id={saved.id}) ===")
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("规则保存失败: %s", exc)
            return 1

    # 规则 JSON 输出（独立分支，避免上面引用未定义变量）
    import json as _json

    print("=== 生成的规则 JSON ===")
    print(_json.dumps(rule, ensure_ascii=False, indent=2))
    print(f"=== Provider: {meta.get('provider')} / {meta.get('mode')} ===")
    if meta.get("reason"):
        print(f"=== 状态说明: {meta['reason']} ===")

    if a.rules_only:
        return 0

    try:
        from rule_executor import execute_rule

        summary = execute_rule(rule, base_output=a.output, provider_info=meta)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("规则执行失败: %s", exc)
        return 1
    print("=== 执行摘要 ===")
    print(f"run_id: {summary['run_id']}")
    print(f"指标: {_json.dumps(summary['metrics'], ensure_ascii=False)}")
    print(f"阶段: {_json.dumps([s['name'] for s in summary['stages']], ensure_ascii=False)}")
    print(f"产物: {_json.dumps([x['name'] for x in summary['artifacts']], ensure_ascii=False)}")
    return 0


def _cmd_extract(argv: List[str]) -> int:
    """文档 → 结构化输出。"""
    import argparse as _ap
    import json as _json

    p = _ap.ArgumentParser(
        prog="etl_pipeline.py extract",
        description="从 PDF/DOCX/TXT/图片中抽取结构化数据与表格",
        formatter_class=_ap.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("document", nargs="+", help="文档路径（可传多个，批量处理）")
    p.add_argument("--schema-file", default="", help="字段定义文件(每行: *名称:类型:定位别名)")
    p.add_argument("--schema", default="", help="字段定义文本，用 ; 分隔")
    p.add_argument("-o", "--output", default="./output", help="产物输出根目录")
    p.add_argument("--eco", action="store_true",
                   help="节能模式：字段语义定位 + 滑动窗口，只把相关片段送给 LLM")
    p.add_argument("--eco-window", type=int, default=200, help="命中位置前后各取的字符数 N")
    p.add_argument("--eco-max-snippets", type=int, default=24, help="最多送入的片段数")
    p.add_argument("--eco-max-chars", type=int, default=12000, help="送入字符总量上限")
    p.add_argument("--workers", type=int, default=4, help="批量并发数(1-8)")
    p.add_argument("--formats", default="csv,xlsx",
                   help="结构化导出格式，逗号分隔：csv,xlsx")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    a = p.parse_args(argv)
    setup_logging(a.log_level)
    LOGGER.info("文档结构化抽取链路启动 documents=%d eco=%s", len(a.document), a.eco)

    try:
        from pathlib import Path as _Path

        from doc_extract import (
            extract_document, extract_documents_batch, parse_schema,
        )
        from exporters import export_extraction, export_extraction_batch, new_run_dir

        if a.schema_file:
            schema_text = _Path(a.schema_file).read_text(encoding="utf-8")
        else:
            schema_text = a.schema.replace(";", "\n")
        schema = parse_schema(schema_text)
        cfg = {
            "window": a.eco_window, "max_snippets": a.eco_max_snippets,
            "max_chars": a.eco_max_chars, "max_workers": a.workers,
        }
        formats = [x.strip().lower() for x in a.formats.split(",") if x.strip()]
        run_dir = new_run_dir(a.output)
        if len(a.document) == 1:
            result, meta = extract_document(
                a.document[0], schema, eco=a.eco, eco_config=cfg,
            )
            artifacts = export_extraction(result, run_dir, formats=formats)
            print("=== 结构化输出 ===")
            print(_json.dumps(result["fields"], ensure_ascii=False, indent=2))
            print(f"表格数: {len(result['tables'])} | 字段数: {len(result['fields'])}")
            if meta.get("eco"):
                print(f"节能: {_json.dumps(meta['eco'], ensure_ascii=False)}")
            batch_meta = None
        else:
            batch = extract_documents_batch(
                a.document, schema, eco=a.eco, eco_config=cfg,
                max_workers=a.workers,
            )
            artifacts = export_extraction_batch(batch, run_dir, formats=formats)
            batch_meta = batch["meta"]
            meta = batch_meta
            print("=== 批量抽取汇总 ===")
            print(_json.dumps(batch_meta, ensure_ascii=False, indent=2))
            for item in batch["errors"]:
                print(f"失败: {item['name']} -> {item['error']}")
        print(f"Provider: {meta.get('provider')}/{meta.get('mode')}"
              if batch_meta is None else "Provider: batch")
        print(f"产物: {_json.dumps([x.name for x in artifacts], ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("文档抽取失败: %s", exc)
        return 1
    return 0


def _cmd_web(argv: List[str]) -> int:
    """启动本地在线查看服务。"""
    import argparse as _ap

    p = _ap.ArgumentParser(
        prog="etl_pipeline.py web", description="启动本地 Web 工作台"
    )
    p.add_argument("--host", default="127.0.0.1", help="绑定地址")
    p.add_argument("--port", type=int, default=8765, help="端口")
    p.add_argument("-o", "--output", default="./output", help="产物根目录")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    a = p.parse_args(argv)
    setup_logging(a.log_level)
    try:
        from webapp import run_server

        return run_server(host=a.host, port=a.port, base_output=a.output)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Web 服务启动失败: %s", exc)
        return 1


def _cmd_rules(argv: List[str]) -> int:
    """规则库管理：list / show / delete / save。"""
    import argparse as _ap
    import json as _json
    from pathlib import Path as _Path

    p = _ap.ArgumentParser(
        prog="etl_pipeline.py rules",
        description="管理已保存的处理规则（保存后可用 nl --rule NAME 直接复用）",
    )
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="列出全部已保存规则")
    show = sub.add_parser("show", help="打印某条规则的完整 JSON")
    show.add_argument("ref", help="规则 ID 或名称")
    delete = sub.add_parser("delete", help="删除某条规则")
    delete.add_argument("ref", help="规则 ID 或名称")
    save = sub.add_parser("save", help="把规则 JSON 文件存入规则库")
    save.add_argument("name", help="规则名称")
    save.add_argument("path", help="规则 JSON 文件路径")
    a = p.parse_args(argv)

    try:
        from rule_store import (
            RuleStoreError, delete_rule, list_rules, resolve_rule, save_rule,
        )

        if a.action == "list":
            items = list_rules()
            if not items:
                print("规则库为空。可用 `nl --save-rule NAME` 或 `rules save NAME 文件` 保存。")
            for item in items:
                print(
                    f"{item.id}  {item.name}  [{item.op_count} 算子] "
                    f"{item.updated_at}  {item.intent[:48]}"
                )
            return 0
        if a.action == "show":
            item = resolve_rule(a.ref)
            if item is None:
                print(f"找不到规则：{a.ref}")
                return 1
            print(_json.dumps(item.rule, ensure_ascii=False, indent=2))
            return 0
        if a.action == "delete":
            item = resolve_rule(a.ref)
            if item is None:
                print(f"找不到规则：{a.ref}")
                return 1
            delete_rule(item.id)
            print(f"已删除：{item.name} ({item.id})")
            return 0
        # save
        raw = _json.loads(_Path(a.path).read_text(encoding="utf-8"))
        item = save_rule(a.name, raw)
        print(f"已保存：{item.name} (id={item.id}, {item.op_count} 算子)")
        return 0
    except (RuleStoreError, OSError, ValueError) as exc:
        LOGGER.error("规则库操作失败: %s", exc)
        return 1


def _cmd_showcase(argv: List[str]) -> int:
    """生成场景化展示模板样例 HTML（演示 / 汇报 / 使用说明）。"""
    import argparse as _ap

    p = _ap.ArgumentParser(
        prog="etl_pipeline.py showcase",
        description="生成场景化展示模板样例 HTML（离线自包含单页）",
    )
    p.add_argument("--output-dir", default="output/showcase", help="输出目录")
    a = p.parse_args(argv)
    try:
        from display_showcase import main as showcase_main

        return showcase_main(["--output-dir", a.output_dir])
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("展示样例生成失败: %s: %s", type(exc).__name__, exc)
        return 1


def _cmd_llm_check(argv: List[str]) -> int:
    """打印 LLM 配置诊断；输出绝不包含 API Key 的值。"""
    import argparse as _ap
    import json as _json

    p = _ap.ArgumentParser(
        prog="etl_pipeline.py llm-check",
        description="诊断 LLM 配置与在线/离线状态（不输出 API Key）",
    )
    p.parse_args(argv)
    try:
        from llm_provider import diagnose_config

        print(_json.dumps(diagnose_config(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("LLM 配置诊断失败: %s", exc)
        return 1


def _dispatch_subcommand(argv_list: List[str]) -> Optional[int]:
    """命中新子命令则执行并返回状态码；未命中返回 None 交给原 parser。"""
    if not argv_list or argv_list[0] not in _SUBCOMMANDS:
        return None
    cmd, rest = argv_list[0], argv_list[1:]
    if cmd == "nl":
        return _cmd_nl(rest)
    if cmd == "extract":
        return _cmd_extract(rest)
    if cmd == "llm-check":
        return _cmd_llm_check(rest)
    if cmd == "showcase":
        return _cmd_showcase(rest)
    if cmd == "rules":
        return _cmd_rules(rest)
    if cmd == "cleanup":
        from artifact_cleanup import main as cleanup_main
        return cleanup_main(rest)
    return _cmd_web(rest)


def main(argv: Optional[List[str]] = None) -> int:
    # 前缀分派：仅当首个参数严格等于新子命令时接管，其余原样交给旧 parser
    normalized = list(sys.argv[1:] if argv is None else argv)
    dispatched = _dispatch_subcommand(normalized)
    if dispatched is not None:
        return dispatched

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    setup_logging(args.log_level, args.log_file)
    LOGGER.info("ETL 管道启动")

    try:
        config = PipelineConfig(
            input_path=args.input,
            input_format=args.format,
            output_dir=args.output,
            sqlite_table=args.table,
            sqlite_query=args.query,
            excel_sheet=args.sheet,
            drop_duplicates=not args.no_dedup,
            fill_missing=args.fill,
            outlier_method=args.outlier,
            outlier_action=args.outlier_action,
            outlier_threshold=args.outlier_threshold,
            group_by=args.group_by,
            agg_funcs=tuple(_parse_agg(args.agg)),
            pivot_index=args.pivot_index,
            pivot_columns=args.pivot_columns,
            pivot_values=args.pivot_values,
            pivot_agg=args.pivot_agg,
            time_column=args.time_col,
            resample_freq=args.resample,
            resample_agg=tuple(_parse_agg(args.resample_agg)),
            encoding=args.encoding,
            log_level=args.log_level,
            log_file=args.log_file,
            max_charts=args.max_charts,
            normalize_columns=not args.no_normalize_cols,
            clean_text=not args.no_clean_text,
            drop_empty_cols=not args.no_drop_empty,
            drop_constant_cols=args.drop_constant,
        )
    except ValueError as exc:
        LOGGER.error("配置校验失败: %s", exc)
        return 2

    try:
        report_path = run_pipeline(config)
    except Exception as exc:  # noqa: BLE001 -- 顶层兜底，确保异常被日志记录
        LOGGER.exception("管道执行失败: %s", exc)
        return 1

    LOGGER.info("管道执行成功，报告: %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

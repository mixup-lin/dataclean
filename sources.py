#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多数据源读取与多表算子支持。

约束
----
- 每个数据源独立做路径安全校验，数量上限 5。
- join 前把关联键统一为字符串，避免 int / str 混用导致关联为空。
- 右表行数上限 20 万；结果行数超过主表 10 倍时中止，避免笛卡尔积打爆内存。
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ops.errors import RuleValidationError
from ops.guards import validate_source_path

LOGGER = logging.getLogger("etl")

MAX_SOURCES = 5
MAX_JOIN_ROWS = 200_000
MAX_EXPANSION = 10.0

# ------------------------------------------------------------
# 上传落盘（Web 多数据源）
# ------------------------------------------------------------
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
UPLOAD_FORMATS = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".json": "json",
    ".xlsx": "excel", ".xls": "excel",
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
}
_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fa5._-]+")


def guess_format(filename: str) -> str:
    """按扩展名推断格式；未知扩展名返回空串。"""
    return UPLOAD_FORMATS.get(Path(filename).suffix.lower(), "")


def safe_filename(filename: str, default: str = "upload.csv") -> str:
    """清洗上传文件名：去掉路径分隔符与危险字符，保留中文与扩展名。"""
    name = Path(str(filename or "")).name
    name = _SAFE_NAME.sub("_", name).strip("._") or default
    if not Path(name).suffix:
        name += ".csv"
    return name[-80:]


def save_upload(
    filename: str,
    data: bytes,
    *,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """把上传内容落盘到 uploads/，返回可直接作为 source 的描述。

    安全约束：扩展名白名单、体积上限、文件名清洗、随机前缀防覆盖，
    最终路径限制在 uploads 目录内（不做目录穿越）。
    """
    fmt = guess_format(filename)
    if not fmt:
        raise RuleValidationError([
            f"不支持的上传类型：{filename!r}，可选 "
            f"{sorted(set(UPLOAD_FORMATS))}"
        ])
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise RuleValidationError(["上传内容为空"])
    if len(data) > MAX_UPLOAD_BYTES:
        raise RuleValidationError([
            f"文件 {filename} 超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限"
        ])
    base = Path(root) if root else UPLOAD_DIR
    base.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    target = (base / f"{uuid.uuid4().hex[:8]}_{name}").resolve()
    if base.resolve() not in target.parents:
        raise RuleValidationError([f"非法上传路径：{filename}"])
    target.write_bytes(bytes(data))
    LOGGER.info("上传文件已保存: %s (%d 字节, 格式=%s)", target.name, len(data), fmt)
    return {
        "path": str(target),
        "format": fmt,
        "name": name,
        "size": len(data),
    }


def build_config(source: Dict[str, Any], output_dir: str = "./output") -> Any:
    """把单个 source 描述映射为 PipelineConfig（仅读取相关字段）。"""
    from etl_pipeline import PipelineConfig

    return PipelineConfig(
        input_path=str(source.get("path", "")),
        input_format=str(source.get("format", "csv")),
        output_dir=output_dir,
        sqlite_table=str(source.get("sqlite_table", "data") or "data"),
        sqlite_query=source.get("sqlite_query"),
        excel_sheet=source.get("excel_sheet"),
        encoding=str(source.get("encoding", "utf-8") or "utf-8"),
    )


def read_source(
    source: Dict[str, Any], allowed_root: Optional[Path] = None
) -> pd.DataFrame:
    """读取单个数据源；附加表不做缺失填充，避免破坏关联键。"""
    from etl_pipeline import read_data

    validate_source_path(source, allowed_root)
    return read_data(build_config(source))


def read_sources(
    sources: Dict[str, Any], allowed_root: Optional[Path] = None
) -> Dict[str, pd.DataFrame]:
    """读取除 main 外的全部附加数据源，返回 {别名: DataFrame}。"""
    if not isinstance(sources, dict) or not sources:
        return {}
    if len(sources) > MAX_SOURCES:
        raise RuleValidationError([f"数据源数量 {len(sources)} 超过上限 {MAX_SOURCES}"])
    frames: Dict[str, pd.DataFrame] = {}
    for alias, src in sources.items():
        if alias == "main":
            continue
        if not isinstance(src, dict):
            raise RuleValidationError([f"sources.{alias} 必须是对象"])
        frames[str(alias)] = read_source(src, allowed_root)
        LOGGER.info("附加数据源 %s 读取完成: %d 行", alias, len(frames[str(alias)]))
    return frames


def _check_keys(frame: pd.DataFrame, cols: List[str], label: str) -> None:
    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise RuleValidationError([f"{label} 关联键不存在：{missing}"])


def join_frames(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    how: str = "left",
    left_on: Optional[List[str]] = None,
    right_on: Optional[List[str]] = None,
    suffixes: Tuple[str, str] = ("", "_右表"),
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """执行 join 并返回统计（匹配数 / 未匹配数 / 膨胀保护）。"""
    left_keys = [str(c) for c in (left_on or [])]
    right_keys = [str(c) for c in (right_on or left_keys)]
    if not left_keys:
        raise RuleValidationError(["join 缺少 left_on"])
    if len(left_keys) != len(right_keys):
        raise RuleValidationError(["join 的 left_on 与 right_on 数量必须一致"])
    _check_keys(left, left_keys, "主表")
    _check_keys(right, right_keys, "右表")
    if len(right) > MAX_JOIN_ROWS:
        raise RuleValidationError(
            [f"右表 {len(right)} 行超过上限 {MAX_JOIN_ROWS}，请先聚合或筛选"]
        )

    left = left.copy()
    right = right.copy()
    for lk, rk in zip(left_keys, right_keys):
        # 键对齐：统一字符串并去首尾空格，避免 "1001" / 1001 / "1001 " 三类写法错配
        left[lk] = left[lk].astype("string").str.strip()
        right[rk] = right[rk].astype("string").str.strip()

    # 合并前先估算膨胀风险：右表键重复度极高时直接中止，避免 merge 撑爆内存
    try:
        dup_max = int(right.groupby(right_keys, dropna=False).size().max() or 1)
    except (ValueError, KeyError):
        dup_max = 1
    if dup_max > MAX_EXPANSION:
        raise RuleValidationError([
            f"右表关联键最多重复 {dup_max} 次（上限 {int(MAX_EXPANSION)}），"
            "疑似键不唯一，已中止；请先按关联键去重或聚合"
        ])

    merged = left.merge(
        right, how=how, left_on=left_keys, right_on=right_keys,
        suffixes=suffixes, indicator="_merge",
    )
    stats = {
        "how": how,
        "matched": int((merged["_merge"] == "both").sum()),
        "unmatched_left": int((merged["_merge"] == "left_only").sum()),
        "unmatched_right": int((merged["_merge"] == "right_only").sum()),
    }
    merged = merged.drop(columns=["_merge"])
    stats["rows"] = len(merged)
    stats["match_rate"] = round(
        stats["matched"] / max(len(left), 1) * 100, 2
    )
    if len(merged) > max(len(left), 1) * MAX_EXPANSION:
        raise RuleValidationError([
            f"关联结果膨胀到 {len(merged)} 行（主表 {len(left)} 行），"
            "疑似关联键不唯一，已中止"
        ])
    return merged, stats


def union_frames(
    left: pd.DataFrame, right: pd.DataFrame
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """按同名列纵向合并，避免列顺序差异导致错位。"""
    common = [c for c in left.columns if c in right.columns]
    if not common:
        raise RuleValidationError(["union 的两个表没有同名列"])
    out = pd.concat([left[common], right[common]], ignore_index=True)
    stats = {"rows": len(out), "columns": common, "dropped_columns": [
        c for c in right.columns if c not in common
    ]}
    return out, stats

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一导出层：结果数据、规则 JSON、文档抽取结果与执行摘要。

产物约定
--------
每次执行在 ``output/run_<时间戳>_<随机码>/`` 下落地：
  result.csv / result.xlsx / result.json  处理结果（按规则 formats）
  rule.json                             规则 JSON（可回看 / 可编辑后重跑）
  extraction.json / extraction.csv       文档结构化抽取结果
  execution_summary.json                执行摘要（供 Web 列表与分页读取）
  quality_report_*.html                 静态质量报告（由核心管道生成后复制）

所有产物名均为固定白名单文件名，下载路由据此做路径安全校验。
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("etl")

RUN_ID_RE = re.compile(r"^run_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")
MAX_TABLES = 6
TABLE_FILE_RE = re.compile(r"^table_[1-6]\.csv$")
ARTIFACT_WHITELIST = {
    "result.csv", "result.xlsx", "result.json", "rule.json",
    "extraction.json", "extraction.csv", "extraction.xlsx",
    "extraction_batch.json", "extraction_batch.csv", "extraction_batch.xlsx",
    "execution_summary.json",
}
ARTIFACT_WHITELIST |= {f"table_{i}.csv" for i in range(1, MAX_TABLES + 1)}


@dataclass
class Artifact:
    """一个可下载产物的描述。"""

    name: str
    path: str
    kind: str            # data | rule | extraction | summary | report
    size: int
    label: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_run_dir(base: str = "./output") -> Path:
    """创建并返回带时间戳的运行目录，避免同秒覆盖。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"run_{stamp}_{uuid.uuid4().hex[:6]}"
    path = Path(base) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def fixed_number(value: float) -> str:
    """定点小数字符串（禁用科学计数法），去除尾随零。"""
    if value == 0:
        return "0"
    s = f"{value:.15f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _json_scalar(value: Any) -> str:
    """把单个值序列化为 JSON 片段，浮点数一律定点表示。"""
    if value is None:
        return "null"
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return "null"
        return fixed_number(f)
    if isinstance(value, (np.ndarray, list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if pd.isna(value):
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def records_to_json(df: pd.DataFrame, indent: int = 2) -> str:
    """按 records 形式输出 JSON，数值不使用科学计数法。"""
    frame = df.copy()
    frame.columns = [str(c) for c in frame.columns]
    keys = [json.dumps(str(c), ensure_ascii=False) for c in frame.columns]
    pad = " " * indent
    rows: List[str] = []
    # itertuples 避免先构造 N 个 dict，显著降低大表的内存与耗时
    for values in frame.itertuples(index=False, name=None):
        parts = [
            f"{pad}{pad}{k}: {_json_scalar(v)}" for k, v in zip(keys, values)
        ]
        rows.append("{\n" + ",\n".join(parts) + f"\n{pad}}}")
    return "[\n" + ",\n".join(rows) + "\n]"


def _csv_float(value: float) -> str:
    return fixed_number(value)


def _sheet_name(name: str, used: set) -> str:
    bad = set('[]:*?/\\')
    base = "".join(ch for ch in str(name) if ch not in bad).strip() or "表"
    base = base[:28]
    candidate, i = base, 2
    while candidate in used:
        candidate = f"{base[:26]}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def export_dataframe(
    df: pd.DataFrame,
    run_dir: Path,
    formats: List[str],
    extra_sheets: Optional[List[Any]] = None,
) -> List[Artifact]:
    """导出结果数据到 csv / xlsx / json，返回产物清单。

    ``extra_sheets`` 为 [(表名, DataFrame)]，仅写入 Excel 的附加 sheet，
    让一次执行即可拿到「主结果 + 附表 + 关联统计」的完整工作簿。
    """
    out: List[Artifact] = []
    for fmt in dict.fromkeys(formats):
        if fmt not in ("csv", "xlsx", "json"):
            LOGGER.warning("忽略不支持的导出格式: %s", fmt)
            continue
        try:
            if fmt == "csv":
                path = run_dir / "result.csv"
                # utf-8-sig 便于 Excel 直接打开中文不乱码
                df.to_csv(
                    path, index=False, encoding="utf-8-sig", float_format=_csv_float
                )
                label = "处理结果 CSV"
            elif fmt == "json":
                path = run_dir / "result.json"
                path.write_text(records_to_json(df), encoding="utf-8")
                label = "处理结果 JSON"
            else:
                path = run_dir / "result.xlsx"
                used: set = set()
                with pd.ExcelWriter(path, engine="openpyxl") as writer:
                    df.to_excel(
                        writer, index=False, sheet_name=_sheet_name("处理结果", used)
                    )
                    for name, frame in (extra_sheets or [])[:MAX_TABLES]:
                        if isinstance(frame, pd.DataFrame) and not frame.empty:
                            frame.to_excel(
                                writer, index=False,
                                sheet_name=_sheet_name(name, used),
                            )
                label = "处理结果 Excel（含附表）"
            out.append(
                Artifact(path.name, str(path), "data", path.stat().st_size, label)
            )
            LOGGER.info("导出 %s: %s (%d 字节)", fmt, path.name, path.stat().st_size)
        except Exception as exc:  # noqa: BLE001 - 单格式失败不影响其它格式
            LOGGER.warning("导出 %s 失败: %s", fmt, type(exc).__name__)
    return out


def export_tables(
    tables: List[pd.DataFrame], run_dir: Path, limit: int = MAX_TABLES
) -> List[Artifact]:
    """导出附加视图（分组汇总 / 关联统计等）为 table_N.csv，供结果页切换查看。"""
    out: List[Artifact] = []
    for idx, frame in enumerate(tables[:limit], start=1):
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        name = f"table_{idx}.csv"
        path = run_dir / name
        try:
            frame.to_csv(
                path, index=False, encoding="utf-8-sig", float_format=_csv_float
            )
        except Exception as exc:  # noqa: BLE001 - 附表失败不影响主结果
            LOGGER.warning("导出附表 %s 失败: %s", name, type(exc).__name__)
            continue
        out.append(
            Artifact(name, str(path), "view", path.stat().st_size, f"附表 {idx}")
        )
    return out


def export_rule(rule: Dict[str, Any], run_dir: Path) -> Artifact:
    path = run_dir / "rule.json"
    path.write_text(
        json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return Artifact(path.name, str(path), "rule", path.stat().st_size, "规则 JSON")


def _frame_to_artifacts(
    df: pd.DataFrame,
    base_name: str,
    run_dir: Path,
    formats: Sequence[str],
    label: str,
    extra_sheets: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Artifact]:
    """把一张宽表导出为 CSV / Excel，返回产物描述。

    CSV 用 ``utf-8-sig``（Excel 直接打开不乱码）；Excel 首行即字段名，
    多表格时追加独立 sheet。openpyxl 缺失时只跳过 xlsx，不影响 CSV。
    """
    out: List[Artifact] = []
    fmts = {str(f).lower() for f in (formats or ("csv",))}
    if "csv" in fmts:
        path = run_dir / f"{base_name}.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig", float_format=_csv_float)
        out.append(Artifact(path.name, str(path), "extraction",
                            path.stat().st_size, f"{label} CSV"))
    if "xlsx" in fmts or "excel" in fmts:
        try:
            path = run_dir / f"{base_name}.xlsx"
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="抽取结果", index=False)
                for sheet_name, frame in (extra_sheets or {}).items():
                    frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
            out.append(Artifact(path.name, str(path), "extraction",
                                path.stat().st_size, f"{label} Excel"))
        except ImportError:
            LOGGER.warning("未安装 openpyxl，已跳过 %s.xlsx", base_name)
    return out


def export_extraction(
    result: Dict[str, Any],
    run_dir: Path,
    formats: Sequence[str] = ("csv",),
) -> List[Artifact]:
    """导出单份结构化抽取结果：JSON 全量 + 横向宽表 CSV / Excel。

    ``formats`` 由 Web/CLI 传入（默认仅 csv，保持旧调用兼容）。
    """
    out: List[Artifact] = []
    json_path = run_dir / "extraction.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out.append(
        Artifact(
            json_path.name, str(json_path), "extraction",
            json_path.stat().st_size, "结构化抽取 JSON",
        )
    )
    df = extraction_to_frame(result)
    if df is not None and not df.empty:
        sheets: Dict[str, pd.DataFrame] = {}
        for i, t in enumerate((result.get("tables") or [])[1:], 2):
            cols = [str(c) for c in (t.get("columns") or [])]
            rows = t.get("rows") or []
            if cols and rows and isinstance(t, dict):
                sheets[f"表格{i}"] = pd.DataFrame(
                    [[r[j] if j < len(r) else "" for j in range(len(cols))] for r in rows],
                    columns=cols,
                )
        out.extend(_frame_to_artifacts(
            df, "extraction", run_dir, formats, "结构化抽取", sheets
        ))
    return out


def extract_fields_width(result: Dict[str, Any]) -> Dict[str, str]:
    """字段 → 值的横向字典（供单份与批量宽表共用）。"""
    fields = result.get("fields") or {}
    if not isinstance(fields, dict):
        return {}
    return {str(k): _flatten_value(v) for k, v in fields.items()}


def batch_to_frame(batch: Dict[str, Any]) -> pd.DataFrame:
    """批量结果 → 合并宽表：第一行为字段名，其后每行一份文档。

    列顺序固定为 schema 字段（按定义顺序）+ 运行元数据列，字段名横向铺开，
    便于直接透视/筛选；缺失字段留空，失败文档也占一行并标注原因。
    """
    schema = batch.get("schema") or []
    columns = [str(s.get("name")) for s in schema if isinstance(s, dict) and s.get("name")]
    for doc in batch.get("documents") or []:
        fields = extract_fields_width(doc.get("result") or {})
        for key in fields:
            if key not in columns:
                columns.append(key)
    meta_cols = ["_源文件", "_状态", "_错误"]
    rows: List[Dict[str, str]] = []
    for doc in batch.get("documents") or []:
        row = {c: "" for c in columns + meta_cols}
        row.update(extract_fields_width(doc.get("result") or {}))
        row["_源文件"] = str(doc.get("name") or "")
        row["_状态"] = str(doc.get("status") or "")
        row["_错误"] = str(doc.get("error") or "")
        rows.append(row)
    return pd.DataFrame(rows, columns=columns + meta_cols)


def export_extraction_batch(
    batch: Dict[str, Any],
    run_dir: Path,
    formats: Sequence[str] = ("csv", "xlsx"),
) -> List[Artifact]:
    """导出批量抽取：全量 JSON + 合并宽表 CSV / Excel（每行一份文档）。"""
    out: List[Artifact] = []
    json_path = run_dir / "extraction_batch.json"
    payload = {
        "schema": batch.get("schema") or [],
        "meta": batch.get("meta") or {},
        "errors": batch.get("errors") or [],
        "documents": [
            {
                "path": d.get("path"), "name": d.get("name"),
                "status": d.get("status"), "error": d.get("error"),
                "result": d.get("result"),
                "eco": (d.get("meta") or {}).get("eco"),
            }
            for d in batch.get("documents") or []
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    out.append(Artifact(json_path.name, str(json_path), "extraction",
                        json_path.stat().st_size, "批量抽取全量 JSON"))
    frame = batch_to_frame(batch)
    if not frame.empty:
        out.extend(_frame_to_artifacts(
            frame, "extraction_batch", run_dir, formats, "批量抽取汇总"
        ))
    return out


def extraction_to_frame(result: Dict[str, Any], wide: bool = True) -> Optional[pd.DataFrame]:
    """把抽取结果转为 DataFrame：优先首个表格；否则用**字段名横向**宽表。

    ``wide=True``（默认）时列即字段名、第一行为字段名、第二行是取值，
    批量场景下每份文档再占一行；旧的「字段/值」纵向形态可用 ``wide=False`` 取回。
    """
    tables = result.get("tables") or []
    if isinstance(tables, list) and tables:
        t = tables[0]
        if isinstance(t, dict):
            cols = [str(c) for c in (t.get("columns") or [])]
            rows = t.get("rows") or []
            if cols and rows:
                normalized = [
                    [r[i] if i < len(r) else "" for i in range(len(cols))]
                    for r in rows
                ]
                return pd.DataFrame(normalized, columns=cols)
    fields = extract_fields_width(result)
    if not fields:
        return None
    if wide:
        return pd.DataFrame([fields])
    return pd.DataFrame(
        [{"字段": str(k), "值": v} for k, v in fields.items()]
    )


def _flatten_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def write_summary(summary: Dict[str, Any], run_dir: Path) -> Artifact:
    path = run_dir / "execution_summary.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return Artifact(
        path.name, str(path), "summary", path.stat().st_size, "执行摘要 JSON"
    )


def copy_report(report_path: str, run_dir: Path) -> Optional[Artifact]:
    """把核心管道生成的 HTML 报告复制进本次运行目录，便于统一查看/下载。"""
    src = Path(report_path)
    if not src.exists():
        return None
    dst = run_dir / src.name
    if src.resolve() != dst.resolve():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return Artifact(
        dst.name, str(dst), "report", dst.stat().st_size, "HTML 质量报告"
    )


def list_runs(base: str = "./output") -> List[Dict[str, Any]]:
    """扫描输出目录，返回按时间倒序的执行摘要列表（供 Web 首页）。"""
    root = Path(base)
    if not root.exists():
        return []
    items: List[Dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir() or not RUN_ID_RE.match(child.name):
            continue
        f = child / "execution_summary.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["run_id"] = child.name
            items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    items.sort(key=lambda d: str(d.get("run_id", "")), reverse=True)
    return items


def load_run(run_id: str, base: str = "./output") -> Optional[Dict[str, Any]]:
    if not RUN_ID_RE.match(run_id or ""):
        return None
    f = Path(base) / run_id / "execution_summary.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        data["run_id"] = run_id
        return data
    except (json.JSONDecodeError, OSError):
        return None


def resolve_artifact(
    run_id: str, name: str, base: str = "./output", allow_report: bool = True
) -> Optional[Path]:
    """下载路由的路径安全校验：run_id 与文件名双重白名单，杜绝目录穿越。"""
    if not RUN_ID_RE.match(run_id or ""):
        return None
    if name in ARTIFACT_WHITELIST:
        allowed = True
    elif allow_report and re.match(
        r"^(quality|task)_report_[0-9]{8}_[0-9]{6}\.html$", name
    ):
        allowed = True
    else:
        allowed = False
    if not allowed or "/" in name or "\\" in name or ".." in name:
        return None
    path = Path(base) / run_id / name
    return path if path.is_file() else None


def read_page(
    csv_path: str, page: int = 1, page_size: int = 20
) -> Dict[str, Any]:
    """按页读取结果 CSV，避免把 2 万行全量塞进内存 / DOM。"""
    from etl_pipeline import _fmt_num

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    skip = (page - 1) * page_size
    try:
        chunk = pd.read_csv(
            csv_path, encoding="utf-8-sig", skiprows=range(1, skip + 1),
            nrows=page_size, dtype=str, keep_default_na=False,
        )
        with open(csv_path, encoding="utf-8-sig") as fh:
            total = max(0, sum(1 for _ in fh) - 1)
    except (OSError, pd.errors.ParserError, ValueError) as exc:
        LOGGER.warning("结果分页读取失败: %s", type(exc).__name__)
        return {"columns": [], "rows": [], "total": 0, "page": page}
    columns = [str(c) for c in chunk.columns]
    rows = [
        {c: _fmt_num_value(chunk.iloc[i][c], _fmt_num) for c in columns}
        for i in range(len(chunk))
    ]
    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, math.ceil(total / page_size)),
    }


def _fmt_num_value(value: str, fmt: Any) -> str:
    """CSV 以字符串读取后，尽量还原数值并走统一格式化；文本原样返回。"""
    if value == "":
        return "—"
    try:
        num = float(value)
        if not math.isfinite(num):
            return str(value)
        return fmt(num)
    except (TypeError, ValueError):
        return str(value)

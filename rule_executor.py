#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则执行器：read → clean → transform → steps → 任务画像 → 导出 / 报告。

输入：经 ``rules.assert_valid_rule`` 校验的规则 JSON（可含多数据源 sources）。
输出：执行摘要 Dict（含阶段耗时、指标、产物清单、报告路径、任务画像），并落盘到
``output/run_<时间戳>_<随机码>/``，供 CLI 打印、Web 在线查看与下载复用。

执行器只调用既有核心函数，不重造清洗轮子；steps 是有序、逐步记录、单步失败
可跳过而不中断整体的受限算子管线，全部算子在白名单内实现，无 eval、无代码生成。
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace as dc_replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from etl_pipeline import (
    clean_data,
    generate_report,
    read_data,
    transform_data,
)
from etl_pipeline import _fmt_num, _quality_score
from exporters import (
    copy_report,
    export_dataframe,
    export_rule,
    export_tables,
    new_run_dir,
    write_summary,
)
from ops import contract
from ops.conditions import condition_columns
from ops.compiler import compile_rule, reconcile_rule_columns
from ops.constants import PCT_DEN_COL, PCT_NUM_COL, aggregate_pairs, pct_alias
from ops.errors import RuleValidationError
from ops.guards import validate_source_path
from ops.registry import get_op
from rules import assert_valid_rule, probe_columns, rule_to_config
from sources import read_sources

LOGGER = logging.getLogger("etl")

# ------------------------------------------------------------
# 兼容别名：算子实现已迁移到 ops.builtin（注册表驱动，避免双份逻辑）
# ------------------------------------------------------------
from ops.builtin.row_ops import apply_filter as _apply_filter  # noqa: F401
from ops.builtin.value_ops import (  # noqa: F401
    apply_derive as _apply_derive, apply_cast as _apply_cast, apply_fill as _apply_fill,
    apply_string as _apply_string, apply_split as _apply_split, apply_concat as _apply_concat,
    apply_date_part as _apply_date_part, apply_bin as _apply_bin,
    apply_assign as _apply_assign, apply_rank as _apply_rank,
)
from ops.builtin.agg_ops import apply_aggregate as _apply_aggregate  # noqa: F401
from ops.builtin.pct_ops import apply_pct as _apply_pct  # noqa: F401
from ops.builtin.reshape_ops import apply_pivot as _apply_pivot  # noqa: F401


# ============================================================
# 单步执行
# ============================================================


def apply_step(
    df: pd.DataFrame,
    step: Dict[str, Any],
    frames: Optional[Dict[str, pd.DataFrame]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, str]:
    """执行单个算子：查注册表 → 调用算子实现。"""
    op = step.get("op")
    spec = get_op(str(op))
    if spec is None:
        raise RuleValidationError([f"未知算子：{op}"])
    return spec.execute(df, step, frames, context)


def apply_steps(
    df: pd.DataFrame,
    steps: List[Dict[str, Any]],
    frames: Optional[Dict[str, pd.DataFrame]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """顺序执行 steps；单步失败记录为 skipped 并继续，保证流程可完成。"""
    logs: List[Dict[str, Any]] = []
    for idx, step in enumerate(steps):
        started = time.monotonic()
        before = len(df)
        rec: Dict[str, Any] = {
            "index": idx,
            "op": step.get("op"),
            "rows_before": before,
            "status": "ok",
        }
        try:
            df, note = apply_step(df, step, frames, context)
            rec.update(
                rows_after=len(df), note=note,
                elapsed=round(time.monotonic() - started, 3),
            )
            LOGGER.info(
                "规则步骤 %d [%s] 完成: %d → %d 行 (%s)",
                idx, step.get("op"), before, len(df), note,
            )
        except Exception as exc:  # noqa: BLE001 - 单步失败不中断整体
            rec.update(
                status="skipped", rows_after=before,
                note=f"已跳过：{type(exc).__name__}: {exc}",
                elapsed=round(time.monotonic() - started, 3),
            )
            LOGGER.warning(
                "规则步骤 %d [%s] 跳过：%s: %s",
                idx, step.get("op"), type(exc).__name__, exc,
            )
        logs.append(rec)
    return df, logs


# ============================================================
# 多源与附属视图
# ============================================================
def _resolve_sources(
    rule: Dict[str, Any], allowed_root: Optional[Path]
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """校验并规范化全部数据源路径，返回 (主源, 全部源)。"""
    raw_sources = rule.get("sources")
    sources: Dict[str, Any] = {}
    if isinstance(raw_sources, dict) and raw_sources:
        sources = {str(k): v for k, v in raw_sources.items() if isinstance(v, dict)}
    main = sources.get("main") or rule.get("source") or {}
    if not isinstance(main, dict) or not main.get("path"):
        raise RuleValidationError(["缺少主数据源"])
    resolved: Dict[str, Dict[str, Any]] = {}
    for alias, src in sources.items():
        path = validate_source_path(src, allowed_root)
        resolved[alias] = {**src, "path": str(path)}
    main_path = validate_source_path(main, allowed_root)
    resolved.setdefault("main", {**main, "path": str(main_path)})
    resolved["main"] = {**resolved["main"], "path": str(main_path)}
    return resolved["main"], resolved


def _probe_all_columns(resolved: Dict[str, Dict[str, Any]]) -> List[str]:
    columns: List[str] = []
    for src in resolved.values():
        try:
            columns += probe_columns(src)
        except Exception as exc:  # noqa: BLE001 - 探测失败仅放弃列校验
            LOGGER.warning("列名探测失败：%s", type(exc).__name__)
    return list(dict.fromkeys(columns))


def _aux_views(
    base_df: pd.DataFrame,
    profile: Any,
    join_stats: List[Dict[str, Any]],
) -> List[Tuple[str, pd.DataFrame]]:
    """生成附表：维度汇总 + 关联统计，均限制行数避免报告过大。"""
    views: List[Tuple[str, pd.DataFrame]] = []
    dim = getattr(profile, "primary_dim", "")
    if isinstance(dim, str) and dim and dim in base_df.columns:
        summary = (
            base_df.groupby(dim, dropna=False)
            .size()
            .reset_index(name="条数")
            .sort_values("条数", ascending=False)
        )
        if len(summary) > 1:
            views.append((f"{dim}汇总", summary.head(200)))
    if join_stats:
        rows = []
        for item in join_stats:
            rows.append({
                "数据源": item.get("alias", ""),
                "关联方式": item.get("how", ""),
                "结果行数": item.get("rows", 0),
                "匹配行数": item.get("matched", 0),
                "匹配率(%)": item.get("match_rate", 0),
                "主表未匹配": item.get("unmatched_left", 0),
                "右表未匹配": item.get("unmatched_right", 0),
            })
        views.append(("关联统计", pd.DataFrame(rows)))
    return views


# ============================================================
# 主执行入口
# ============================================================
def execute_rule(
    rule: Dict[str, Any],
    *,
    base_output: str = "./output",
    provider_info: Optional[Dict[str, Any]] = None,
    allowed_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """按规则执行完整流程，返回执行摘要并落盘全部产物。

    输入  : 规则 JSON（version 1.0）、输出根目录、可选 Provider 元信息。
    输出  : Dict{run_id, rule, stages, metrics, artifacts, report_name, task, ...}。
    """
    started_all = time.monotonic()
    rule = dict(rule)
    main_source, resolved = _resolve_sources(rule, allowed_root)
    columns = _probe_all_columns(resolved)

    # 编译/降解：列名对齐 → 契约校验 → 强类型可执行步骤
    plan = compile_rule(rule, columns)
    reconcile_warnings = plan.warnings
    rule["steps"] = plan.steps
    rule["version"] = plan.version
    assert_valid_rule(rule, columns)
    for w in reconcile_warnings:
        LOGGER.warning("规则规整：%s", w)

    # 路径以校验后的绝对路径重写，防止相对路径 / 软链接歧义
    rule["source"] = dict(main_source)
    if isinstance(rule.get("sources"), dict) and rule["sources"]:
        rule["sources"] = {k: dict(v) for k, v in resolved.items()}

    run_dir = new_run_dir(base_output)
    config = rule_to_config(rule, output_dir=str(run_dir))
    stages: List[Dict[str, Any]] = []

    def stage(name: str, fn: Any, *args: Any) -> Any:
        t0 = time.monotonic()
        result = fn(*args)
        stages.append({"name": name, "elapsed": round(time.monotonic() - t0, 3)})
        LOGGER.info("阶段完成 [%s] 耗时 %.3fs", name, stages[-1]["elapsed"])
        return result

    df_raw = stage("读取数据源", read_data, config)

    # 条件判断必须基于原始的「空 / 非空」语义：清洗阶段的缺失填充会把
    # 「未成交」填成中位数、把文本空值填成 "Unknown"，从而抹掉两类区分。
    # 这里只记录原始缺失位置（不覆盖文本归一/类型转换的结果）。
    filter_cols = _filter_columns(rule, columns)
    missing_masks = _missing_masks(df_raw, filter_cols)

    df, clean_report = stage("数据清洗", clean_data, df_raw, config)
    safety_warnings: List[str] = []
    if len(df_raw) and len(df) < max(1, int(len(df_raw) * 0.1)):
        LOGGER.warning("清洗导致行数骤降 %d/%d，启用安全回退", len(df), len(df_raw))
        safety_warnings.append(
            f"清洗后仅剩 {len(df)}/{len(df_raw)} 行（fill_missing=drop 或异常值删除过度），"
            "已自动改用不删行的清洗配置重跑，避免结果为空"
        )
        config = dc_replace(config, fill_missing="median", outlier_action="report")
        df, clean_report = clean_data(df_raw, config)

    # 还原原始缺失位置，保证 isna / notna / 数值比较不被填充值污染
    _restore_missing_masks(df, missing_masks)

    transform_results = stage("统计与聚合", transform_data, df, config)

    extra_frames: Dict[str, pd.DataFrame] = {}
    need_sources = _needs_external_frames(rule)
    if need_sources:
        extra_frames = stage("读取附加数据源", read_sources, resolved, allowed_root)

    step_logs: List[Dict[str, Any]] = []
    step_context: Dict[str, Any] = {}
    base_df = df
    if rule.get("steps"):
        t0 = time.monotonic()
        df, step_logs = apply_steps(df, rule["steps"], extra_frames, step_context)
        stages.append(
            {
                "name": "整理与统计步骤",
                "elapsed": round(time.monotonic() - t0, 3),
                "steps": step_logs,
            }
        )
        LOGGER.info("阶段完成 [整理与统计步骤] 共 %d 步", len(step_logs))
        # 单步失败不再静默：结果页必须提示「结果可能不完整」，避免用户把
        # 被跳过的分析步骤当成执行成功（历史上表现为「结果获取失败」）。
        for rec in step_logs:
            if rec.get("status") != "ok":
                safety_warnings.append(
                    f"第 {int(rec.get('index', 0)) + 1} 步 [{rec.get('op')}] 未执行："
                    f"{rec.get('note', '')}；最终结果可能不完整，请检查规则中的列名与参数"
                )
    else:
        stages.append({"name": "整理与统计步骤", "elapsed": 0.0, "steps": []})

    # steps 可能改变列集合，刷新摘要以保证报告与最终结果一致
    try:
        transform_results["summary"] = df.describe(include="all")
    except Exception as exc:  # noqa: BLE001 - 保留原摘要兜底
        LOGGER.warning("刷新统计摘要失败，沿用清洗后摘要：%s", type(exc).__name__)

    # 报告体积保护：按高基数列分组时附加表可能有数千行 / 数百列
    grouped = transform_results.get("grouped")
    if isinstance(grouped, pd.DataFrame):
        slim = grouped.head(200)
        if len(slim.columns) > 12:
            slim = slim.iloc[:, :12]
        transform_results["grouped"] = slim
    if any(
        rec.get("op") == "aggregate" and rec.get("status") == "ok"
        for rec in step_logs
    ):
        # steps 已产出汇总结果，不再重复渲染庞大的分组统计表
        transform_results.pop("grouped", None)
    preview = df.head(100)
    if len(preview.columns) > 20:
        preview = preview.iloc[:, :20]
    transform_results["result_table"] = preview.copy()

    join_stats = list(step_context.get("joins", []))

    # ---------- 任务画像 ----------
    profile = stage(
        "任务画像", _build_profile,
        df, rule, step_logs, clean_report, base_df, join_stats,
    )
    aux_views = _aux_views(base_df, profile, join_stats)
    table_artifacts = export_tables([frame for _, frame in aux_views], run_dir)
    profile.tables = profile.tables[:1] + [
        {"name": name, "frame": frame, "note": "附表"} for name, frame in aux_views
    ]

    # ---------- 报告 ----------
    report_path = ""
    if rule.get("output", {}).get("report", True):
        t0 = time.monotonic()
        report_path = generate_report(df, clean_report, transform_results, config)
        stages.append(
            {"name": "生成质量报告", "elapsed": round(time.monotonic() - t0, 3)}
        )

    task_report_path = ""
    if rule.get("output", {}).get("report", True):
        t0 = time.monotonic()
        task_report_path = _render_task_report(
            profile, run_dir, rule, join_stats, clean_report, step_logs
        )
        if task_report_path:
            stages.append(
                {"name": "生成任务报告", "elapsed": round(time.monotonic() - t0, 3)}
            )

    # ---------- 导出 ----------
    formats = rule.get("output", {}).get("formats", ["csv"])
    artifacts: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    artifacts += [
        a.to_dict()
        for a in export_dataframe(df, run_dir, formats, extra_sheets=aux_views)
    ]
    artifacts += [a.to_dict() for a in table_artifacts]
    artifacts.append(export_rule(rule, run_dir).to_dict())
    if report_path:
        rep = copy_report(report_path, run_dir)
        if rep:
            artifacts.append(rep.to_dict())
    if task_report_path:
        artifacts.append(
            {
                "name": Path(task_report_path).name,
                "path": task_report_path,
                "kind": "task_report",
                "size": Path(task_report_path).stat().st_size,
                "label": "任务报告 HTML",
            }
        )
    stages.append(
        {"name": "导出产物", "elapsed": round(time.monotonic() - t0, 3)}
    )

    raw_missing = clean_report.get("missing_by_col", pd.Series(dtype="int64"))
    raw_missing = raw_missing.reindex(df.columns).fillna(0).astype("int64")
    metrics = {
        "总行数": len(df),
        "总列数": df.shape[1],
        "原始缺失": int(raw_missing.sum()),
        "重复行": clean_report.get("duplicates_removed", 0),
        "异常值": clean_report.get("outliers", {}).get("count", 0),
        "剔除列": len(clean_report.get("dropped_columns", [])),
        "质量评分": _quality_score(df, clean_report),
    }

    task_payload = profile.to_dict()
    task_payload["tables"] = [
        {
            "name": entry.get("name", "结果"),
            "file": "result.csv" if idx == 0 else (
                table_artifacts[idx - 1].name if idx - 1 < len(table_artifacts) else ""
            ),
            "rows": int(len(entry.get("frame", []))),
            "columns": [str(c) for c in getattr(entry.get("frame"), "columns", [])][:24],
            "note": entry.get("note", ""),
        }
        for idx, entry in enumerate(profile.tables)
    ]
    task_payload["join"] = join_stats
    task_payload["multi_source"] = len(resolved) > 1

    result_csv = str(run_dir / "result.csv")
    summary: Dict[str, Any] = {
        "run_id": run_dir.name,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "intent": rule.get("intent", ""),
        "rule": rule,
        "provider": provider_info or {},
        "stages": stages,
        "metrics": metrics,
        "step_logs": step_logs,
        "reconcile_warnings": reconcile_warnings,
        "safety_warnings": safety_warnings,
        "artifacts": artifacts,
        "formats": formats,
        "report_name": Path(report_path).name if report_path else "",
        "task_report_name": Path(task_report_path).name if task_report_path else "",
        "result_csv": result_csv,
        "rows": len(df),
        "columns": len(df.columns),
        "elapsed": round(time.monotonic() - started_all, 3),
        "task": task_payload,
        "source_count": len(resolved),
    }
    artifacts.append(write_summary(summary, run_dir).to_dict())
    summary["artifacts"] = artifacts
    # 摘要写盘两次（第二次包含自身条目），保证列表与详情一致
    write_summary(summary, run_dir)
    LOGGER.info(
        "规则执行完成 run_id=%s 任务=%s 耗时=%.3fs",
        summary["run_id"], task_payload.get("task_type"), summary["elapsed"],
    )
    return summary


def _filter_columns(
    rule: Dict[str, Any], columns: Optional[Sequence[str]] = None
) -> set:
    """收集需要在清洗后还原原始空值的列。

    来源有两个，取并集：
    1. **通用扫描**：递归找出步骤里所有条件型字典引用的列（filter / assign.rules /
       pct.condition / rate.condition / assert.checks / having / cross_column_filter）。
       新增带条件的算子自动受保护，历史上 ``assign`` 未登记 preserve_refs 导致
       「成交价为空 → 未成交」被中位数填充抹平，正是这类漏登记。
    2. **算子声明**：``preserve_refs``（如 date_filter 的日期列）与
       ``meta.preserve_all``（如未指定 subset 的 dropna 需要还原全部列）。
    """
    cols: set = set()
    for step in rule.get("steps") or []:
        if not isinstance(step, dict):
            continue
        cols.update(condition_columns(step))
        spec = get_op(str(step.get("op")))
        if spec is None:
            continue
        refs: List[str] = []
        if spec.preserve_refs is not None:
            refs = [c for c in (spec.preserve_refs(step) or [])
                    if isinstance(c, str) and c]
        if not refs and spec.meta.get("preserve_all") and columns:
            refs = list(columns)
        cols.update(refs)
    return cols


_PSEUDO_MISSING = {
    "", "nan", "none", "null", "na", "n/a", "<na>", "nat",
}


def _missing_masks(df_raw: pd.DataFrame, cols: set) -> Dict[str, pd.Series]:
    """记录清洗前每列的原始缺失位置（含空串 / 伪缺失文本）。"""
    masks: Dict[str, pd.Series] = {}
    for col in cols:
        if not isinstance(col, str) or col not in df_raw.columns:
            continue
        series = df_raw[col]
        mask = series.isna()
        if series.dtype == object or pd.api.types.is_string_dtype(series):
            text = series.astype("string").str.strip().str.lower()
            mask = mask | text.isin(_PSEUDO_MISSING).fillna(False)
        masks[col] = mask.fillna(False).astype(bool)
    return masks


def _restore_missing_masks(
    df: pd.DataFrame, masks: Dict[str, pd.Series]
) -> None:
    """清洗后把原始缺失位置重新置空，同时保留文本归一 / 类型转换的成果。

    与「整列还原原始值」相比，这样既恢复了 isna/notna 的语义，又不会把
    「 鼓楼区 」这类前后空格、已解析成 datetime 的日期列退回脏字符串。
    """
    for col, mask in masks.items():
        if col not in df.columns:
            continue
        aligned = mask.reindex(df.index, fill_value=False).fillna(False).astype(bool)
        if not bool(aligned.any()):
            continue
        try:
            df[col] = df[col].mask(aligned)
        except (TypeError, ValueError):  # pragma: no cover - 极少数扩展 dtype
            df[col] = df[col].astype(object).where(~aligned, np.nan)
        LOGGER.info("还原原始缺失值：%s（%d 个位置）", col, int(aligned.sum()))


def _needs_external_frames(rule: Dict[str, Any]) -> bool:
    external = ("join", "union", "anti_join", "semi_join")
    return any(
        isinstance(s, dict) and s.get("op") in external
        for s in (rule.get("steps") or [])
    )


def _build_profile(
    df: pd.DataFrame,
    rule: Dict[str, Any],
    step_logs: List[Dict[str, Any]],
    clean_report: Dict[str, Any],
    base_df: pd.DataFrame,
    join_stats: List[Dict[str, Any]],
) -> Any:
    """构建任务画像；失败时降级为最小明细画像，不阻断导出。"""
    try:
        from task_profile import build_profile

        return build_profile(
            df, rule, step_logs=step_logs, clean_report=clean_report,
            base_df=base_df, join_stats=join_stats,
        )
    except Exception as exc:  # noqa: BLE001 - 画像失败不应阻断主流程
        LOGGER.warning("任务画像构建失败，降级为明细视图：%s", type(exc).__name__)
        from task_profile import fallback_profile

        return fallback_profile(df)


def _render_task_report(
    profile: Any,
    run_dir: Path,
    rule: Dict[str, Any],
    join_stats: List[Dict[str, Any]],
    clean_report: Dict[str, Any],
    step_logs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    try:
        from report_task import render_task_report

        return render_task_report(
            profile, run_dir, rule=rule, join_stats=join_stats,
            clean_report=clean_report, step_logs=step_logs,
        )
    except Exception as exc:  # noqa: BLE001 - 报告失败不影响结果数据
        LOGGER.warning("任务报告渲染失败：%s: %s", type(exc).__name__, exc)
        return ""


def execute_rule_dict(
    payload: Dict[str, Any], base_output: str = "./output"
) -> Dict[str, Any]:
    """从 Web 表单字典（intent / source / sources / columns / rule）执行。"""
    from rules import generate_rules_from_dict

    rule = payload.get("rule")
    meta: Dict[str, Any] = {}
    if isinstance(rule, dict) and rule.get("version"):
        meta = {"provider": "manual", "mode": "offline", "degraded": False}
    else:
        rule, meta = generate_rules_from_dict(payload)
    return execute_rule(rule, base_output=base_output, provider_info=meta)

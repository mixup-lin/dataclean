#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务画像：把「规则 steps + 执行结果形态」识别为任务类型，并规划 KPI / 图表 / 表格。

设计要点
--------
- 任务类型只由规则与结果结构推导，**不猜测用户原话**，因此可复现、可离线复用。
- 图表只放聚合后的小数据（KPI ≤ 6、图表 ≤ 3、表格行数由调用方截断），
  保证报告与结果页体积可控。
- 画像失败不阻断执行：``fallback_profile`` 始终可用。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from etl_pipeline import _fmt_num

LOGGER = logging.getLogger("etl")

TASK_LABELS = {
    "proportion": "占比分析",
    "ranking": "排名分析",
    "trend": "趋势分析",
    "distribution": "分布分析",
    "correlation": "相关分析",
    "pivot": "透视分析",
    "join": "多表合并",
    "summary": "汇总统计",
    "detail": "明细查看",
}
MAX_ROWS = 200


@dataclass
class TaskProfile:
    """跨模块共享的任务画像契约。"""

    task_type: str
    title: str
    primary_dim: str = ""
    value_col: str = ""
    kpis: List[Dict[str, Any]] = field(default_factory=list)
    charts: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    panels: List[Dict[str, Any]] = field(default_factory=list)
    conclusion: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的摘要（表格只保留元信息，不携带 DataFrame）。"""
        return {
            "task_type": self.task_type,
            "label": TASK_LABELS.get(self.task_type, self.task_type),
            "title": self.title,
            "primary_dim": self.primary_dim,
            "value_col": self.value_col,
            "kpis": self.kpis,
            "charts": self.charts,
            "panels": self.panels,
            "conclusion": self.conclusion,
            "note": self.note,
            "tables": [
                {
                    "name": str(t.get("name", "")),
                    "note": str(t.get("note", "")),
                    "rows": int(len(t.get("frame", [])))
                    if isinstance(t.get("frame"), pd.DataFrame) else int(t.get("rows", 0)),
                }
                for t in self.tables
            ],
        }


# ============================================================
# 小工具
# ============================================================
def _num(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(num) or math.isinf(num) else num


def _kpi(label: str, value: Any, hint: str = "", unit: str = "") -> Dict[str, Any]:
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float):
        value = round(value, 4)
    return {"label": str(label), "value": value, "hint": str(hint), "unit": str(unit)}


def _pct(part: float, total: float) -> float:
    return round(part / total * 100, 2) if total else 0.0


def _is_pct(name: str) -> bool:
    return any(k in str(name) for k in ("占比", "比例", "百分比", "pct"))


def _numeric_cols(df: pd.DataFrame, exclude: Sequence[str] = ()) -> List[str]:
    skip = set(str(c) for c in exclude)
    return [
        str(c) for c in df.select_dtypes(include="number").columns
        if str(c) not in skip
    ]


def _count_col(df: pd.DataFrame, exclude: Sequence[str] = ()) -> str:
    preferred = ("合计", "条数", "平均值", "金额", "价", "数量")
    numeric = _numeric_cols(df, exclude)
    for key in preferred:
        for col in numeric:
            if key in col:
                return col
    return numeric[0] if numeric else ""


def _overall_numeric(df: pd.DataFrame, exclude: Sequence[str] = ()) -> str:
    numeric = _numeric_cols(df, exclude)
    if not numeric:
        return ""
    # 跳过排名列与分子分母，优先选择取值范围更大的数值列
    for col in numeric:
        if "排名" in col:
            continue
        return col
    return numeric[0]


def _find_col(df: pd.DataFrame, *names: str) -> str:
    columns = [str(c) for c in df.columns]
    for name in names:
        if name in columns:
            return name
    for name in names:
        for col in columns:
            if name in col:
                return col
    return ""


def _dim_col(df: pd.DataFrame, exclude: Sequence[str] = ()) -> str:
    """选出用于分组展示的列：优先低基数文本列。"""
    skip = set(str(c) for c in exclude)
    candidates: List[Tuple[int, str]] = []
    for col in df.columns:
        name = str(col)
        if name in skip or _is_pct(name) or name in ("分子", "分母"):
            continue
        series = df[col]
        if series.dtype.kind not in ("O", "U", "S", "b") and not isinstance(
            series.dtype, pd.CategoricalDtype
        ):
            continue
        uniques = series.nunique(dropna=True)
        if 1 < uniques <= max(30, len(df) * 0.6):
            candidates.append((uniques, name))
    if candidates:
        return sorted(candidates)[0][1]
    return ""


def _top_pairs(
    df: pd.DataFrame, label_col: str, value_col: str, limit: int = 12,
    ascending: bool = False,
) -> List[List[Any]]:
    if not label_col or label_col not in df.columns or value_col not in df.columns:
        return []
    frame = df[[label_col, value_col]].copy()
    frame["_v"] = pd.to_numeric(frame[value_col], errors="coerce").fillna(0.0)
    frame = frame.dropna(subset=[label_col]).sort_values("_v", ascending=ascending)
    return [
        [str(row[label_col]), float(row["_v"])]
        for _, row in frame.head(limit).iterrows()
    ]


def _label_pairs(
    df: pd.DataFrame, label_col: str, value_col: str, limit: int = 16
) -> List[List[Any]]:
    """按结果表原始顺序取标签与数值，适合分箱 / 时间等有内在顺序的维度。"""
    if label_col not in df.columns or value_col not in df.columns:
        return []
    out: List[List[Any]] = []
    for _, row in df.head(limit).iterrows():
        label = row[label_col]
        out.append([str(label), _num(row[value_col])])
    return out


def _main_table(df: pd.DataFrame, note: str = "") -> Dict[str, Any]:
    return {"name": "主结果", "frame": df.head(MAX_ROWS).copy(), "note": note}


def _total_values(df: pd.DataFrame, column: str) -> Tuple[float, List[float]]:
    """全量数值口径：图表只取 TopN，但合计/均值/占比分母必须基于完整结果表。"""
    if isinstance(column, str) and column and column in df.columns:
        series = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
        return float(series.sum()), [float(v) for v in series]
    return 0.0, []


# ============================================================
# 任务类型识别
# ============================================================
_JOIN_OPS = ("join", "union", "anti_join", "semi_join")
_AGG_OPS = ("aggregate", "count_rows")


def _declared_is_consistent(
    declared: str,
    steps: Sequence[Dict[str, Any]],
    df: Optional[pd.DataFrame],
) -> bool:
    """显式 task_type 必须与步骤形态相容，否则视为误标。

    LLM 曾把「按小区统计条数」写成 ``task_type: distribution``；若无条件
    采信，报告会给出「分布分析」这种语义错误的结论。这里只对明显矛盾的
    声明做回退，用户显式且合理的声明仍然优先。
    """
    ops = {str(s.get("op")) for s in steps}
    if declared == "distribution":
        if ops & {"bin", "describe", "profile"}:
            return True
        return any(
            s.get("op") == "pct" and s.get("scope") in ("overall", "group")
            for s in steps
        )
    if declared == "proportion":
        return bool(ops & {"pct", "rate"})
    # rate 是比率专用算子：旧规则若误标成 summary，不能盖过结果形态
    if declared in ("summary", "distribution") and "rate" in ops:
        return False
    if declared == "pivot":
        return "pivot" in ops
    if declared == "join":
        return bool(ops & set(_JOIN_OPS))
    if declared == "correlation":
        return "corr" in ops
    if declared == "ranking":
        return bool(ops & {"rank", "sort"})
    if declared == "trend":
        if not (ops & {"date_part", "resample"}):
            return False
        return df is None or _has_time_periods(df, _time_part_columns(steps))
    return True  # summary / detail 宽松，不与任何步骤形态冲突


def _successful_steps(
    rule: Dict[str, Any], step_logs: Optional[Sequence[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """只保留执行成功的步骤。

    画像层不能把 ``skipped`` 的步骤当作分析依据：曾出现「聚合之后的 date_part
    因列已不存在被跳过，画像却仍把它当时间维度」而把分组计数判成趋势分析。
    """
    steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    if not step_logs:
        return steps
    statuses = {
        int(rec["index"]): rec.get("status")
        for rec in step_logs
        if isinstance(rec, dict) and isinstance(rec.get("index"), int)
    }
    return [
        step for idx, step in enumerate(steps)
        if statuses.get(idx, "ok") == "ok"
    ]


def _time_part_columns(steps: Sequence[Dict[str, Any]]) -> List[str]:
    """时间维度列：date_part 的派生列 + resample 的时间轴列。"""
    from ops.constants import DATE_PART_LABELS, normalize_freq_alias

    out: List[str] = []
    for step in steps:
        op = str(step.get("op"))
        col = step.get("column") or step.get("time_column")
        if not isinstance(col, str) or not col:
            continue
        if op == "date_part":
            for part in step.get("parts") or []:
                label = DATE_PART_LABELS.get(str(part))
                if label:
                    out.append(f"{col}_{label}")
            bucket = step.get("bucket")
            if isinstance(bucket, str) and bucket:
                out.append(f"{col}_{bucket}")
        elif op == "resample":
            out.append(f"{col}_{normalize_freq_alias(str(step.get('freq') or 'ME'))}")
    return out


def _bin_columns(steps: Sequence[Dict[str, Any]]) -> List[str]:
    """bin 生成的分箱列名（用于判断聚合结果是否为区间分布）。"""
    out: List[str] = []
    for step in steps:
        if step.get("op") != "bin":
            continue
        into = step.get("into")
        col = step.get("column")
        if isinstance(into, str) and into:
            out.append(into)
        elif isinstance(col, str) and col:
            out.append(f"{col}_分箱")
    return out


def _has_time_periods(df: Optional[pd.DataFrame], cols: Sequence[str]) -> bool:
    """结果里确实存在 >=2 个不同时间周期，才允许判定为趋势。"""
    if not isinstance(df, pd.DataFrame) or df.empty or not cols:
        return False
    for col in cols:
        if col in df.columns and df[col].nunique(dropna=True) >= 2:
            return True
    return False


def detect_task_type(
    rule: Dict[str, Any],
    df: Optional[pd.DataFrame] = None,
    step_logs: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """识别任务类型：一致的显式声明 > 注册表角色 > 执行状态 > 结果形态。

    判定优先级：
    1. 规则顶层 ``task_type``（白名单内且与步骤形态不矛盾时采信；
       明显误标如「分组计数却声明 distribution」会自动回退）；
    2. 只考虑**执行成功**的步骤，按注册表 ``role`` 找最后一个 analysis 算子；
    3. 趋势必须同时满足：分组维度是真正的时间维度列，且结果里 >=2 个周期；
    4. 无 analysis 算子时按注册表 ``task_hint`` 回退，否则为 detail。
    """
    from ops.constants import TASK_TYPES
    from ops.registry import get_op

    steps = _successful_steps(rule, step_logs)
    declared = rule.get("task_type")
    if isinstance(declared, str) and declared in TASK_TYPES:
        if _declared_is_consistent(declared, steps, df):
            return declared
        LOGGER.info("已忽略与步骤形态矛盾的 task_type=%r，改用自动推断", declared)

    return _infer_task_type(rule, steps, df)


def _infer_task_type(
    rule: Dict[str, Any],
    steps: List[Dict[str, Any]],
    df: Optional[pd.DataFrame],
) -> str:
    """按注册表角色 + 执行状态 + 结果形态推断任务类型。"""
    from ops.registry import get_op

    ops = [str(s.get("op")) for s in steps]
    if any(op in _JOIN_OPS for op in ops):
        return "join"
    if "rate" in ops:
        return "proportion"
    if "pct" in ops:
        return "proportion"
    if "pivot" in ops:
        return "pivot"

    analysis_steps = [
        s for s in steps
        if (spec := get_op(str(s.get("op")))) is not None and spec.role == "analysis"
    ]
    if analysis_steps:
        last = analysis_steps[-1]
        last_op = str(last.get("op"))
        spec = get_op(last_op)
        hint = spec.task_hint if spec else ""
        if last_op in _AGG_OPS:
            time_cols = set(_time_part_columns(steps))
            bin_cols = set(_bin_columns(steps))
            by_cols = {str(c) for c in _as_cols(last.get("by"))}
            if by_cols & bin_cols:
                return "distribution"
            time_dims = sorted(by_cols & time_cols)
            if time_dims and _has_time_periods(df, time_dims):
                return "trend"
            if "sort" in ops or "having" in ops or "group_rank" in ops:
                return "ranking"
            return hint or "summary"
        if last_op == "resample":
            axis = _time_part_columns([last])
            if not isinstance(df, pd.DataFrame) or _has_time_periods(df, axis):
                return "trend"
            return "summary"
        if last_op == "corr":
            return "correlation"
        return hint or "summary"

    if "derive" in ops:
        numeric = _numeric_cols(df) if isinstance(df, pd.DataFrame) else []
        return "correlation" if len(numeric) >= 2 else "summary"
    # 通用回退：按注册表声明（后面的步骤更接近最终结果）
    for op in reversed(ops):
        spec = get_op(op)
        if spec is not None and spec.task_hint:
            return spec.task_hint
    return "detail"


# ============================================================
# 各任务画像
# ============================================================
def _pct_step(rule: Dict[str, Any]) -> Dict[str, Any]:
    for step in rule.get("steps") or []:
        if isinstance(step, dict) and step.get("op") == "pct":
            return step
    return {}


def _as_cols(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    return [str(value)] if value not in (None, "") else []


def _last_step(rule: Dict[str, Any], op: str) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for step in rule.get("steps") or []:
        if isinstance(step, dict) and step.get("op") == op:
            found = step
    return found


def _aggregate_by(rule: Dict[str, Any]) -> List[str]:
    return [c for c in _as_cols(_last_step(rule, "aggregate").get("by")) if c]


def _donut_data(pairs: List[List[Any]], limit: int = 6) -> List[List[Any]]:
    if len(pairs) <= limit:
        return pairs
    head = pairs[: limit - 1]
    rest = sum(v for _, v in pairs[limit - 1:])
    return head + [["其他", round(rest, 2)]]


def _rate_profile(df: pd.DataFrame, rule: Dict[str, Any], **_: Any) -> TaskProfile:
    """rate 算子专用画像：一行给出两类条数、目标率与两类比值。

    ``numerator`` / ``denominator`` 决定目标率的口径，因此「成交率」与
    「未成交率」共用同一套面板，只需按规则声明的口径取分子分母。
    """
    step = _last_step(rule, "rate") or {}
    true_label = str(step.get("true_label") or "命中")
    false_label = str(step.get("false_label") or "未命中")
    numerator = str(step.get("numerator") or "hit").strip().lower()
    denominator = str(step.get("denominator") or "total").strip().lower()
    num_label = false_label if numerator == "miss" else true_label
    num_count_col = f"{num_label}数"
    den_count_col = {"total": "总数", "hit": f"{true_label}数",
                     "miss": f"{false_label}数"}.get(denominator, "总数")
    alias = str(step.get("alias") or "").strip() or f"{num_label}率"
    true_col, false_col = f"{true_label}数", f"{false_label}数"
    ratio_col = f"{true_label}{false_label}比"
    if true_col not in df.columns or false_col not in df.columns:
        return _detail_profile(df, rule, **_)
    hit = float(pd.to_numeric(df[true_col], errors="coerce").fillna(0).sum())
    miss = float(pd.to_numeric(df[false_col], errors="coerce").fillna(0).sum())
    total = hit + miss
    num_count = miss if numerator == "miss" else hit
    den_count = {"total": total, "hit": hit, "miss": miss}.get(denominator, total)
    rate = round(num_count / den_count * 100, 2) if den_count else 0.0
    complement_rate = round((total - num_count) / total * 100, 2) if total else 0.0
    ratio = round(hit / miss, 4) if miss else 0.0
    dim = next((c for c in _as_cols(step.get("by")) if c in df.columns), "")
    title = (f"{dim + ' ' if dim else ''}{alias}"
             + ("" if alias.endswith("分析") else "分析"))
    return TaskProfile(
        task_type="proportion",
        title=title,
        primary_dim=dim,
        value_col=alias if alias in df.columns else ratio_col,
        kpis=[
            _kpi(alias, rate, f"{num_label}数 / {den_count_col}", "%"),
            _kpi(f"{true_label}数", int(hit), f"{step.get('column', '')} 满足条件"),
            _kpi(f"{false_label}数", int(miss), f"{step.get('column', '')} 不满足条件"),
            _kpi("样本总量", int(total), "筛选后参与比较的全部记录"),
            _kpi(f"{true_label}/{false_label}", ratio, "两类条数之比"),
            _kpi(f"{false_label if numerator == 'hit' else true_label}率",
                 complement_rate, "互补类 / 总数", "%"),
        ],
        charts=[{
            "kind": "donut", "title": f"{true_label} 与 {false_label} 构成",
            "data": [[true_label, hit], [false_label, miss]],
        }],
        tables=[_main_table(df, f"{alias}结果表：两类条数 / 总数 / {alias} / {ratio_col}")],
        conclusion=(
            f"共 {_fmt_num(total)} 条，{true_label} {_fmt_num(hit)} 条、"
            f"{false_label} {_fmt_num(miss)} 条，{alias} {rate:.2f}%；"
            f"{true_label}/{false_label} = {ratio:.4f}。"
        ),
        note=(
            f"{alias} = {num_count_col} / {den_count_col} × 100%；{ratio_col} = "
            f"{true_label}数 / {false_label}数。分母保留筛选后的全部样本，"
            "判空列的原始空值在执行前已还原。"
        ),
    )


def _proportion_profile(
    df: pd.DataFrame, rule: Dict[str, Any], **_: Any
) -> TaskProfile:
    if _last_step(rule, "rate"):
        return _rate_profile(df, rule, **_)
    step = _pct_step(rule)
    scope = str(step.get("scope") or "")
    by_cols = _as_cols(step.get("by"))
    within_cols = _as_cols(step.get("within"))
    alias = _find_col(df, "组内占比", "成交占比", "占比")
    if not alias:
        alias = next((str(c) for c in df.columns if _is_pct(c)), "")
    dim = next((c for c in by_cols if c in df.columns), "")
    within = next((c for c in within_cols if c in df.columns), "")

    # 条件口径：pct 直接产出单行「分子 / 分母 / 占比」汇总
    if (scope == "condition" or len(df) == 1) and {"分子", "分母"} <= set(df.columns):
        numerator = _num(df["分子"].iloc[0])
        denominator = _num(df["分母"].iloc[0])
        ratio = _num(df[alias].iloc[0]) if alias in df.columns else _pct(numerator, denominator)
        return TaskProfile(
            task_type="proportion",
            title="条件占比分析",
            primary_dim="条件",
            value_col=alias or "占比",
            kpis=[
                _kpi("总体占比", round(ratio, 2), "命中 / 全部", "%"),
                _kpi("命中条数", int(numerator), "满足条件的记录"),
                _kpi("样本总量", int(denominator), "参与比较的全部记录"),
                _kpi("未命中", int(max(denominator - numerator, 0)), "分母 - 分子"),
            ],
            charts=[{
                "kind": "donut", "title": "命中与未命中构成",
                "data": [["命中", numerator], ["未命中", max(denominator - numerator, 0)]],
            }],
            tables=[_main_table(df, "条件占比汇总（分子 / 分母 / 占比）")],
            conclusion=(
                f"满足条件的记录 {_fmt_num(numerator)} 条，占全部"
                f" {_fmt_num(denominator)} 条的 {ratio:.1f}%。"
            ),
            note="占比 = 满足条件行数 / 全部行数 × 100%，分母保留清洗后的全部样本。",
        )

    has_parts = {"分子", "分母"} <= set(df.columns) and bool(dim)
    shares: List[List[Any]] = []
    counts: List[List[Any]] = []
    ratio_series = pd.Series(dtype="float64")
    total = 0.0
    if has_parts:
        num = pd.to_numeric(df["分子"], errors="coerce").fillna(0.0)
        den = pd.to_numeric(df["分母"], errors="coerce").fillna(0.0)
        ratio_series = (num / den.where(den != 0) * 100).fillna(0.0)
        ordered = pd.DataFrame({
            "_label": df[dim].astype(str).to_numpy(),
            "_num": num.to_numpy(),
            "_pct": ratio_series.to_numpy(),
        }).sort_values("_pct", ascending=False)
        for _, row in ordered.head(12).iterrows():
            shares.append([row["_label"], round(float(row["_pct"]), 2)])
            counts.append([row["_label"], float(row["_num"])])
        total = float(num.sum()) if scope == "group" else float(
            den.max() if len(den) else num.sum()
        )
    elif alias in df.columns:
        label_col = dim or _dim_col(df, exclude=(alias, "分子", "分母"))
        shares = _top_pairs(df, label_col, alias, limit=12) if label_col else []
        counts = list(shares)
        dim = label_col
        full_total, _all = _total_values(df, alias)
        total = full_total or sum(v for _, v in shares)

    group_scope = scope == "group"
    top = shares[0] if shares else ["—", 0.0]
    cr3 = sum(v for _, v in shares[:3])
    charts: List[Dict[str, Any]] = []

    if group_scope:
        max_share = round(float(ratio_series.max()) if len(ratio_series) else top[1], 2)
        avg_share = round(float(ratio_series.mean()) if len(ratio_series) else 0.0, 2)
        groups = int(df[dim].nunique(dropna=True)) if dim else int(len(df))
        kpis = [
            _kpi("分组数", groups, f"按 {dim} 分组" if dim else "分组数"),
            _kpi("明细行数", int(len(df)), f"含 {within or '组内维度'} 的明细"),
            _kpi("最大组内占比", max_share, "单组内的最高占比", "%"),
            _kpi("平均组内占比", avg_share, "各组内占比均值", "%"),
        ]
        title = f"{dim}组内占比分析" if dim else "组内占比分析"
        if has_parts:
            grouped = pd.DataFrame({
                "_label": df[dim].astype(str).to_numpy(),
                "_num": pd.to_numeric(df["分子"], errors="coerce").fillna(0).to_numpy(),
            }).groupby("_label", as_index=False)["_num"].sum()
            grouped = grouped.sort_values("_num", ascending=False)
            charts.append({
                "kind": "hbar", "title": f"{dim}记录数 Top 10",
                "data": [[str(r["_label"]), float(r["_num"])]
                         for _, r in grouped.head(10).iterrows()],
            })
        conclusion = (
            f"{len(df)} 行明细按 {dim or '分组'} 分为 {groups} 组，"
            f"以 {within or '组内维度'} 归一；最大组内占比 {max_share:.1f}%，"
            f"平均 {avg_share:.1f}%。"
        )
    else:
        groups = int(len(df))
        kpis = [
            _kpi("分组数", groups, "不同的分类个数"),
            _kpi(f"Top1 · {top[0]}", round(top[1], 2), "占全部比例", "%"),
            _kpi("Top1 数值", counts[0][1] if counts else 0, "最高分组的计数/金额"),
            _kpi("CR3", round(cr3, 2), "前三名合计占比", "%"),
            _kpi("合计", total, "全部分组之和"),
        ]
        title = f"{dim}占比分析" if dim else "构成占比分析"
        conclusion = (
            f"共 {groups} 个{dim or '分组'}，合计 {_fmt_num(total)}；"
            f"Top1 为 {top[0]}（占 {top[1]:.1f}%），Top3 合计占 {cr3:.1f}%。"
        )

    if shares:
        charts.insert(0, {
            "kind": "donut", "title": "构成占比（Top 6）",
            "data": _donut_data(shares),
        })
        if not group_scope:
            charts.append({
                "kind": "pareto" if step.get("cumulative") else "hbar",
                "title": f"{dim or '分组'}占比排名",
                "data": shares,
            })

    return TaskProfile(
        task_type="proportion",
        title=title,
        primary_dim=dim,
        value_col=alias or "占比",
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "占比结果表：分子 / 分母 / 占比可直接导出")],
        conclusion=conclusion,
        note="构成占比以全量合计为分母；组内占比以同组合计为分母；支持累计（帕累托）。",
    )


def _ranking_profile(df: pd.DataFrame, rule: Dict[str, Any], **_: Any) -> TaskProfile:
    dim = next((c for c in _aggregate_by(rule) if c in df.columns), "") or _dim_col(df)
    value_col = _count_col(df, exclude=(dim,))
    pairs = _top_pairs(df, dim, value_col, limit=12) if dim and value_col else []
    full_total, _all = _total_values(df, value_col)
    total = full_total or sum(v for _, v in pairs)
    top = pairs[0] if pairs else ["—", 0.0]
    top3 = sum(v for _, v in pairs[:3])
    kpis = [
        _kpi("参与排名", int(len(df)), "结果行数"),
        _kpi(f"第 1 名 · {top[0]}", top[1], "最高分组的数值"),
        _kpi("前 3 合计", top3, "Top3 数值之和"),
        _kpi("前 3 占比", round(_pct(top3, total), 2), "Top3 / 全部合计", "%"),
    ]
    charts = [{
        "kind": "hbar",
        "title": f"{dim or '分组'}排名（{value_col or '数值'}）",
        "data": pairs,
    }] if pairs else []
    conclusion = (
        f"{dim or '分组'}按 {value_col or '数值'} 排序，"
        f"{top[0]} 以 {_fmt_num(top[1])} 居首，前 3 名合计占 {_pct(sum(v for _, v in pairs[:3]), total):.1f}%。"
    )
    return TaskProfile(
        task_type="ranking",
        title=f"{dim or '分组'}排名分析",
        primary_dim=dim,
        value_col=value_col,
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "排名结果表：已按数值降序排列")],
        conclusion=conclusion,
        note="排名基于结果表数值列；组内排名由 rank 算子在原明细上计算。",
    )


def _trend_profile(df: pd.DataFrame, rule: Dict[str, Any], **_: Any) -> TaskProfile:
    by_cols = [c for c in _aggregate_by(rule) if c in df.columns]
    # 组合时间桶（如 结束时间_年季度）优先于单一分量，避免跨年同季混淆
    period = next((c for c in by_cols if ("年" in c and any(
        token in c for token in ("季", "月", "周", "日")
    ))), "") or next(iter(by_cols), "") or _find_col(
        df, "年", "月", "季度", "周", "日", "星期", "日期", "时间"
    )
    frame = df.dropna(subset=[period]).copy() if period and period in df.columns else df
    # 比值型均价由 aggregate 后的 expr 产出，画像必须跟随该最终列而不是中间和值
    expr_col = next((str(s.get("column")) for s in reversed(rule.get("steps") or [])
                     if isinstance(s, dict) and s.get("op") == "expr"
                     and str(s.get("column")) in frame.columns), "")
    value_col = expr_col or _count_col(frame, exclude=(period,))
    pairs = _label_pairs(frame, period, value_col, limit=36) if period and value_col else []
    peak = max(pairs, key=lambda p: p[1]) if pairs else ["—", 0.0]
    is_average = any(token in value_col for token in ("均价", "单价", "平均", "均值"))
    full_total, all_values = _total_values(frame, value_col)
    overall = (sum(all_values) / len(all_values)) if (is_average and all_values) else full_total
    n_periods = int(len(frame)) or len(pairs)
    change = 0.0
    if len(pairs) >= 2 and pairs[-2][1]:
        change = (pairs[-1][1] - pairs[-2][1]) / abs(pairs[-2][1]) * 100
    kpis = [
        _kpi("期数", n_periods, f"按 {period or '时间'} 聚合"),
        _kpi("峰值期", peak[0], "数值最高的时间桶"),
        _kpi("峰值", peak[1], "最高期的数值"),
        _kpi("末期环比", round(change, 2), "最后一期相对上一期", "%"),
        _kpi("整体均值" if is_average else "累计合计", overall,
             "各期算术平均" if is_average else "全部期次之和"),
    ]
    charts = [{
        "kind": "line",
        "title": f"{value_col or '数量'}随时间变化",
        "data": pairs,
    }] if len(pairs) >= 2 else []
    conclusion = (
        f"共 {len(pairs)} 个时间期，峰值出现在 {peak[0]}（{_fmt_num(peak[1])}），"
        f"末期环比 {change:+.1f}%。"
    )
    note = "时间分量由 date_part 生成，趋势按时间升序排列。"
    if is_average:
        note += "均价按「分子合计 / 分母合计」计算，逐条均值作为对照列一并输出。"
    return TaskProfile(
        task_type="trend",
        title=f"按{period or '时间'}的趋势分析",
        primary_dim=period,
        value_col=value_col,
        kpis=kpis,
        charts=charts,
        tables=[_main_table(frame, "趋势结果表：每行一个时间桶")],
        conclusion=conclusion,
        note=note,
    )


def _distribution_profile(
    df: pd.DataFrame, rule: Dict[str, Any], **_: Any
) -> TaskProfile:
    bin_col = next((c for c in _aggregate_by(rule) if c in df.columns), "") or _find_col(
        df, "分箱"
    )
    if not bin_col:
        bin_col = _dim_col(df)
    value_col = _count_col(df, exclude=(bin_col,))
    pairs = _label_pairs(df, bin_col, value_col, limit=16) if bin_col else []
    full_total, _all = _total_values(df, value_col)
    total = full_total or sum(v for _, v in pairs)
    mode = max(pairs, key=lambda p: p[1]) if pairs else ["—", 0.0]
    kpis = [
        _kpi("区间数", int(len(df)) or len(pairs), "分箱后的段数"),
        _kpi("众数区间", mode[0], "记录最多的区间"),
        _kpi("众数区间占比", round(_pct(mode[1], total), 2), "该区间 / 合计", "%"),
        _kpi("样本量", int(total), "参与统计的记录数"),
    ]
    charts = [{
        "kind": "column",
        "title": f"{bin_col} 分布",
        "data": pairs,
    }] if pairs else []
    conclusion = (
        f"共划分 {len(pairs)} 个区间，{mode[0]} 记录最多（{_fmt_num(mode[1])} 条，"
        f"占 {_pct(mode[1], total):.1f}%）。"
    )
    return TaskProfile(
        task_type="distribution",
        title=f"{bin_col or '数值'}分布分析",
        primary_dim=bin_col,
        value_col=value_col,
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "分布结果表：区间 + 条数")],
        conclusion=conclusion,
        note="区间由 bin 算子生成，等宽分箱可换成等频（quantile）。",
    )


def _pivot_profile(df: pd.DataFrame, rule: Dict[str, Any], **_: Any) -> TaskProfile:
    index_col = str(df.columns[0]) if len(df.columns) else ""
    value_cols = _numeric_cols(df, exclude=(index_col,))[:10]
    rows = [str(v) for v in df[index_col].head(14)] if index_col else []
    matrix = [
        [_num(df.iloc[i][c]) for c in value_cols]
        for i in range(min(len(rows), len(df)))
    ]
    values = [v for line in matrix for v in line]
    kpis = [
        _kpi("行维度数", len(df), f"按 {index_col or '行'} 统计"),
        _kpi("列维度数", len(value_cols), "交叉出来的度量列"),
        _kpi("最大值", max(values) if values else 0, "交叉表最大单元格"),
        _kpi("合计", sum(values), "全部单元格之和"),
    ]
    charts = [{
        "kind": "heatmap", "title": "交叉表热力分布",
        "rows": rows, "cols": value_cols, "matrix": matrix,
    }] if rows and value_cols else []
    top_cell = ""
    if values:
        best = max(range(len(values)), key=lambda i: values[i])
        top_cell = f"{rows[best // len(value_cols)]} × {value_cols[best % len(value_cols)]}"
    return TaskProfile(
        task_type="pivot",
        title=f"{index_col or '行维度'}交叉透视分析",
        primary_dim=index_col,
        value_col=value_cols[0] if value_cols else "",
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "透视结果表：行维度 × 列维度")],
        conclusion=(
            f"交叉表共 {len(df)} 行 × {len(value_cols)} 个度量列，"
            + (f"最大单元格出现在 {top_cell}（{_fmt_num(max(values))}）。" if top_cell else "无有效数值。")
        ),
        note="透视在聚合后的小表上渲染，列数超过 36 时按列合计截断。",
    )


def _join_profile(
    df: pd.DataFrame, rule: Dict[str, Any],
    join_stats: Optional[List[Dict[str, Any]]] = None, **_: Any,
) -> TaskProfile:
    stats = (join_stats or [{}])[0]
    how = str(stats.get("how", "—"))
    dim = _dim_col(df)
    kpis = [
        _kpi("匹配行数", int(_num(stats.get("matched", 0))), "关联到右表的主表行"),
        _kpi("匹配率", _num(stats.get("match_rate", 0)), f"{how} join · 匹配行 / 主表行", "%"),
        _kpi("主表未匹配", int(_num(stats.get("unmatched_left", 0))), "右表无对应记录"),
        _kpi("结果行数", int(len(df)), "关联并整理后的行数"),
        _kpi("数据源数", int(len(rule.get("sources") or {}) or 1), "主表 + 附加表"),
    ]
    matched = _num(stats.get("matched", 0))
    unmatched = _num(stats.get("unmatched_left", 0))
    charts = [{
        "kind": "donut", "title": "关联匹配情况",
        "data": [["已匹配", matched], ["未匹配", unmatched]],
    }] if matched or unmatched else []
    pairs = _top_pairs(df, dim, _count_col(df, exclude=(dim,)), limit=10) if dim else []
    if pairs:
        charts.append({"kind": "hbar", "title": f"{dim}分布（关联后）", "data": pairs})
    conclusion = (
        f"以 {stats.get('how', 'left')} 方式关联，匹配 {_fmt_num(matched)} 行，"
        f"匹配率 {_num(stats.get('match_rate', 0)):.1f}%，主表未匹配 "
        f"{_fmt_num(unmatched)} 行，最终 {_fmt_num(len(df))} 行。"
    )
    return TaskProfile(
        task_type="join",
        title="多表关联结果",
        primary_dim=dim,
        value_col=_count_col(df, exclude=(dim,)),
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "关联结果明细（含右表字段）")],
        conclusion=conclusion,
        note="关联键统一按字符串对齐，结果行数超过主表 10 倍时中止执行。",
    )


def _summary_profile(df: pd.DataFrame, rule: Dict[str, Any], **_: Any) -> TaskProfile:
    dim = next((c for c in _aggregate_by(rule) if c in df.columns), "") or _dim_col(df)
    value_col = _count_col(df, exclude=(dim,))
    pairs = _top_pairs(df, dim, value_col, limit=12) if dim and value_col else []
    # 合计/均值取全量，图表只取 Top12，避免把前 12 名之和当成总量
    full_total, all_values = _total_values(df, value_col)
    values = all_values or [v for _, v in pairs]
    total = full_total if all_values else sum(v for _, v in pairs)
    func = (_as_cols(_last_step(rule, "aggregate").get("func")) or ["count"])[0]
    if func == "mean" and values:
        kpis = [
            _kpi("分组数", int(len(df)), f"按 {dim or '分组'} 汇总"),
            _kpi("分组均值", round(total / len(values), 2), f"{value_col} 的平均"),
            _kpi("最高分组", max(values), f"按 {value_col} 排序第一"),
            _kpi("最低分组", min(values), f"按 {value_col} 排序末位"),
        ]
        headline = f"分组均值 {_fmt_num(round(total / len(values), 2))}"
    else:
        kpis = [
            _kpi("分组数", int(len(df)), f"按 {dim or '分组'} 汇总"),
            _kpi("合计", total, "全部数值之和"),
            _kpi("平均值", round(total / len(values), 2) if values else 0, "分组均值"),
            _kpi("最大值", max(values) if values else 0, "最高分组"),
        ]
        headline = f"合计 {_fmt_num(total)}"
    charts = [{
        "kind": "hbar", "title": f"{dim or '分组'}汇总对比", "data": pairs,
    }] if pairs else []
    top = pairs[0] if pairs else ["—", 0.0]
    return TaskProfile(
        task_type="summary",
        title=f"{dim or '数据'}汇总统计",
        primary_dim=dim,
        value_col=value_col,
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "汇总结果表：每行一个分组")],
        conclusion=(
            f"共 {len(df)} 个{dim or '分组'}，{headline}，"
            f"其中 {top[0]} 最高（{_fmt_num(top[1])}）。"
        ),
        note="汇总结果由 aggregate 算子产出，可直接导出或继续排序筛选。",
    )


def _correlation_profile(
    df: pd.DataFrame, rule: Dict[str, Any], **_: Any
) -> TaskProfile:
    numeric = _numeric_cols(df)[:3]
    kpis: List[Dict[str, Any]] = [_kpi("记录数", int(len(df)), "参与分析的样本")]
    charts: List[Dict[str, Any]] = []
    conclusion = "结果中数值列不足，无法计算相关性。"
    if len(numeric) >= 2:
        x_col, y_col = numeric[0], numeric[1]
        pair = df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce").dropna()
        corr = float(pair.corr().iloc[0, 1]) if len(pair) > 2 else 0.0
        if math.isnan(corr):
            corr = 0.0
        kpis += [
            _kpi("相关系数", round(corr, 4), f"{x_col} ↔ {y_col}"),
            _kpi(f"{x_col} 均值", _num(pair[x_col].mean()), "样本均值"),
            _kpi(f"{y_col} 均值", _num(pair[y_col].mean()), "样本均值"),
        ]
        points = [
            [_num(row[x_col]), _num(row[y_col]), ""]
            for _, row in pair.head(300).iterrows()
        ]
        charts = [{
            "kind": "scatter", "title": f"{x_col} 与 {y_col} 的关系",
            "points": points, "x_label": x_col, "y_label": y_col,
        }]
        direction = "正相关" if corr > 0.3 else ("负相关" if corr < -0.3 else "弱相关")
        conclusion = (
            f"{x_col} 与 {y_col} 的相关系数为 {corr:.3f}，属于{direction}；"
            f"有效样本 {len(pair)} 条。"
        )
    return TaskProfile(
        task_type="correlation",
        title="两列相关分析",
        primary_dim=numeric[0] if numeric else "",
        value_col=numeric[1] if len(numeric) > 1 else "",
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "派生结果表：新增计算列")],
        conclusion=conclusion,
        note="相关系数为皮尔逊系数，仅基于成对非空样本计算。",
    )


def _detail_profile(df: pd.DataFrame, rule: Dict[str, Any], **_: Any) -> TaskProfile:
    rows = int(len(df))
    cells = rows * max(len(df.columns), 1)
    missing = int(df.isna().sum().sum())
    numeric = _numeric_cols(df)
    kpis = [
        _kpi("结果行数", rows, "处理后的记录数"),
        _kpi("列数", int(len(df.columns)), "结果表字段数"),
        _kpi("数值列", len(numeric), "可统计的字段"),
        _kpi("缺失率", round(missing / cells * 100, 2) if cells else 0, "缺失单元格占比", "%"),
    ]
    charts: List[Dict[str, Any]] = []
    dim = _dim_col(df)
    if dim:
        counts = (
            df[dim].astype(str).value_counts().head(10)
        )
        pairs = [[str(k), float(v)] for k, v in counts.items()]
        if pairs:
            charts.append({
                "kind": "hbar", "title": f"{dim} 记录数 Top 10", "data": pairs,
            })
    return TaskProfile(
        task_type="detail",
        title="明细数据结果",
        primary_dim=dim,
        value_col=numeric[0] if numeric else "",
        kpis=kpis,
        charts=charts,
        tables=[_main_table(df, "明细结果（最多展示 200 行）")],
        conclusion=(
            f"共 {_fmt_num(rows)} 行 × {len(df.columns)} 列，"
            f"缺失率 {round(missing / cells * 100, 2) if cells else 0:.1f}%。"
        ),
        note="未识别到特定分析意图，按明细视图展示结果。",
    )


_BUILDERS = {
    "proportion": _proportion_profile,
    "ranking": _ranking_profile,
    "trend": _trend_profile,
    "distribution": _distribution_profile,
    "pivot": _pivot_profile,
    "join": _join_profile,
    "summary": _summary_profile,
    "correlation": _correlation_profile,
    "detail": _detail_profile,
}


def _panel(
    kind: str, title: str, hint: str = "", source: str = "",
    chart_kinds: Sequence[str] = (), count: int = 0,
) -> Dict[str, Any]:
    """声明式面板规格：前端按 kind 分派渲染，空载荷则跳过。"""
    return {
        "kind": kind,
        "title": title,
        "hint": hint,
        "source": source,
        "chart_kinds": list(chart_kinds),
        "count": int(count),
    }


# 任务类型 -> (图表面板标题, 允许的图表类型)
_TASK_CHART = {
    "ranking": ("排名分布", ("hbar", "pareto", "column")),
    "trend": ("时间趋势", ("line", "column")),
    "distribution": ("分布结构", ("column", "hbar")),
    "pivot": ("透视热力", ("heatmap",)),
    "summary": ("汇总对比", ("hbar", "column", "donut")),
    "correlation": ("相关散点", ("scatter",)),
}


def _derive_panels(task_type: str, ops: Sequence[str]) -> List[Dict[str, Any]]:
    """按任务类型 + 实际算子组合，自动编排结果页面板（纯计算，O(#steps)）。"""
    op_set = set(ops or ())
    panels: List[Dict[str, Any]] = [
        _panel("kpi", "指标概览", "任务关键指标", "kpis"),
    ]
    # 多表合并：先展示关联质量
    if task_type == "join" or ({"join", "union"} & op_set):
        panels.append(_panel("join", "关联质量", "匹配率与未匹配行数", "join_stats"))
    # 主图表（按任务类型确定可接受图表类型）
    chart = _TASK_CHART.get(task_type)
    if chart:
        title, kinds = chart
        panels.append(_panel("chart", title, "聚合后的小数据可视化", "charts", kinds))
    # 算子补充：存在 pivot 但主图表不含热力图时，补一个透视面板
    if "pivot" in op_set and not any(
        p["kind"] == "chart" and "heatmap" in p.get("chart_kinds", []) for p in panels
    ):
        panels.append(_panel("chart", "透视热力", "交叉表热力图", "charts", ("heatmap",)))
    # 结果明细表
    table_title = "占比明细" if task_type == "proportion" else "结果明细"
    panels.append(_panel("table", table_title, "分页读取结果表，可切换附表", "tables"))
    return panels


def fallback_profile(df: pd.DataFrame) -> TaskProfile:
    """画像失败时的最小可用结果：保证结果页与报告仍有内容。"""
    frame = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    return TaskProfile(
        task_type="detail",
        title="执行结果",
        primary_dim="",
        value_col="",
        kpis=[_kpi("结果行数", int(len(frame)), "处理后记录数")],
        charts=[],
        tables=[{"name": "主结果", "frame": frame.head(MAX_ROWS).copy(), "note": ""}],
        panels=_derive_panels("detail", ()),
        conclusion=f"共 {_fmt_num(len(frame))} 行 × {len(frame.columns)} 列。",
        note="自动画像不可用，已按明细视图展示。",
    )


def build_profile(
    df: pd.DataFrame,
    rule: Dict[str, Any],
    *,
    step_logs: Optional[List[Dict[str, Any]]] = None,
    clean_report: Optional[Dict[str, Any]] = None,
    base_df: Optional[pd.DataFrame] = None,
    join_stats: Optional[List[Dict[str, Any]]] = None,
) -> TaskProfile:
    """识别任务类型并构建 KPI / 图表 / 表格规划。"""
    frame = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if frame.empty:
        profile = fallback_profile(frame)
        profile.title = "执行结果为空"
        profile.conclusion = "清洗或筛选后没有剩余数据，请放宽筛选条件后重试。"
        profile.note = "结果为空时仅展示执行摘要与规则，不渲染图表。"
        return profile

    task_type = detect_task_type(rule, frame, step_logs)
    builder = _BUILDERS.get(task_type, _detail_profile)
    try:
        profile = builder(
            frame, rule, step_logs=step_logs, clean_report=clean_report,
            base_df=base_df, join_stats=join_stats,
        )
    except Exception:  # noqa: BLE001 - 任意画像异常都退化为明细
        return fallback_profile(frame)

    if not profile.tables:
        profile.tables = [_main_table(frame)]
    profile.kpis = profile.kpis[:6]
    profile.charts = [c for c in profile.charts if c][:3]
    ops = [str(s.get("op")) for s in (rule.get("steps") or []) if isinstance(s, dict)]
    profile.panels = _derive_panels(profile.task_type, ops)
    return profile

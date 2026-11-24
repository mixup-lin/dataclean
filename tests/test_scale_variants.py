# -*- coding: utf-8 -*-
"""规模变体与结构扰动（评估报告 F-04 / F-16）。

验证同一「统计成交率」语义在以下变化下保持不变：

* 规模：10 行 vs 100 行（10× 复制）——命中/未命中条数按规模放大，
  但成交率（语义口径）不变；
* 结构扰动：字段重命名 + 行乱序叠加——规则显式绑定真实列名，
  结果不应因列名/行序变化而漂移。

链路严格走 ``POST /plan → POST /execute → GET /result``。规则由
``FixtureProvider`` 显式注入（不调用真实 LLM），但 ``validate_rule`` /
``evaluate_rule`` / 执行器全部真实运行。所有输出写入 pytest 的 tmp_path，
不污染仓库 output/ 目录。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
import pytest

import webapp
from tests.conftest import extract_rule_from_plan_body, patch_provider
from tests.provider_fixture import FixtureProvider

INTENT = "统计成交率（成交/总数）"

# 基准口径（独立手算）：10 行中 7 条有成交价、3 条为空 → 成交率 70%。
BASE_TOTAL = 10
BASE_HIT = 7
BASE_MISS = 3
BASE_RATE = 70.0

COLS_BASE = ["标的号", "面积(㎡)", "成交价(元)", "结束时间"]
COLS_RENAMED = ["标的代码", "面积", "成交金额", "终止日期"]


# ---------------------------------------------------------------------------
# 数据 / 规则构造
# ---------------------------------------------------------------------------
def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> Path:
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _base_rows() -> List[Dict[str, Any]]:
    """10 行基准数据：前 7 行有成交价，后 3 行为空。"""
    rows: List[Dict[str, Any]] = []
    for i in range(BASE_TOTAL):
        rows.append({
            "标的号": f"LOT-{i:02d}",
            "面积(㎡)": float(10 + i),
            "成交价(元)": None if i >= BASE_HIT else float(1000 + i * 100),
            "结束时间": f"2026-{(i % 12) + 1:02d}-15",
        })
    return rows


def _scaled_rows(times: int = 10) -> List[Dict[str, Any]]:
    """把基准 10 行整体复制 times 份（标的号加后缀保持唯一）。"""
    out: List[Dict[str, Any]] = []
    for copy_idx in range(times):
        for row in _base_rows():
            clone = dict(row)
            clone["标的号"] = f"{row['标的号']}-{copy_idx}"
            out.append(clone)
    return out


def _rate_rule(path: Path, judge_col: str) -> Dict[str, Any]:
    """「统计成交率」的正确规则；judge_col 由用例按实际列名指定。"""
    return {
        "version": "2.0",
        "intent": INTENT,
        "source": {"path": str(path), "format": "csv"},
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": "proportion",
        "steps": [{
            "op": "rate",
            "column": judge_col,
            "condition": {"column": judge_col, "operator": "notna"},
            "true_label": "成交",
            "false_label": "未成交",
            "numerator": "hit",
            "denominator": "total",
            "alias": "成交率",
        }],
        "output": {"formats": ["csv"], "report": True},
    }


# ---------------------------------------------------------------------------
# 全链路执行器
# ---------------------------------------------------------------------------
def _run_chain(
    monkeypatch: Any,
    tmp_path: Path,
    case: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    judge_col: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """走 /plan → /execute → GET /result，返回结果表与执行摘要。"""
    csv_path = _write_csv(tmp_path / f"{case}.csv", columns, rows)
    state = webapp.AppState(str(tmp_path / f"out_{case}"))
    patch_provider(monkeypatch, FixtureProvider(rule=_rate_rule(csv_path, judge_col)))

    plan = webapp.handle_request("POST", "/plan", {}, {
        "intent": [INTENT],
        "source_path": [str(csv_path)],
        "source_format": ["csv"],
        "columns": [",".join(columns)],
        "formats": ["csv"],
        "report": ["1"],
        "generation_path": ["legacy"],
    }, state)
    assert plan.status == 200, plan.body[:1200]
    generated = extract_rule_from_plan_body(plan.body)
    assert [step.get("op") for step in generated["steps"]] == ["rate"]

    execute = webapp.handle_request("POST", "/execute", {}, {
        "rule_json": [json.dumps(generated, ensure_ascii=False)],
    }, state)
    assert execute.status == 302, execute.body[:1200]
    run_id = execute.headers["Location"].split("/")[-1]
    run_dir = Path(state.base_output) / run_id

    page = webapp.handle_request("GET", f"/result/{run_id}", {}, {}, state)
    assert page.status == 200, page.body[:500]

    result = pd.read_csv(run_dir / "result.csv", encoding="utf-8-sig")
    summary = json.loads((run_dir / "execution_summary.json").read_text(encoding="utf-8"))
    return result, summary


def _assert_rate(result: pd.DataFrame, total: int, hit: int, miss: int) -> None:
    """独立手算期望：结果一行，两类条数/总数/成交率精确匹配。"""
    assert len(result) == 1, result
    assert int(result["成交数"].iloc[0]) == hit
    assert int(result["未成交数"].iloc[0]) == miss
    assert int(result["总数"].iloc[0]) == total
    assert float(result["成交率"].iloc[0]) == pytest.approx(
        round(hit / total * 100, 4)
    ), result


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_scale_variants_10_rows_vs_100_rows_keep_rate(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """10 行与 100 行：条数按 10× 放大，成交率保持 70%，语义不变。"""
    small, small_summary = _run_chain(
        monkeypatch, tmp_path, "scale10", COLS_BASE, _base_rows(), "成交价(元)"
    )
    large, large_summary = _run_chain(
        monkeypatch, tmp_path, "scale100", COLS_BASE, _scaled_rows(10), "成交价(元)"
    )

    _assert_rate(small, BASE_TOTAL, BASE_HIT, BASE_MISS)
    _assert_rate(large, BASE_TOTAL * 10, BASE_HIT * 10, BASE_MISS * 10)

    # 规模放大只改变计数，不改变语义口径（成交率一致）。
    assert float(small["成交率"].iloc[0]) == pytest.approx(
        float(large["成交率"].iloc[0])
    )
    assert all(
        step.get("status") == "ok" for step in small_summary["step_logs"]
    ), small_summary["step_logs"]
    assert all(
        step.get("status") == "ok" for step in large_summary["step_logs"]
    ), large_summary["step_logs"]


def test_scale_variants_renamed_columns_and_shuffled_rows_keep_result(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """字段重命名 + 行乱序叠加：显式列绑定使结果仍与基准一致。"""
    rows = _base_rows()
    # 重命名四个字段，并整体倒序模拟行乱序。
    renamed = [
        {
            "标的代码": row["标的号"],
            "面积": row["面积(㎡)"],
            "成交金额": row["成交价(元)"],
            "终止日期": row["结束时间"],
        }
        for row in reversed(rows)
    ]
    result, _ = _run_chain(
        monkeypatch, tmp_path, "renamed_shuffled", COLS_RENAMED, renamed, "成交金额"
    )
    _assert_rate(result, BASE_TOTAL, BASE_HIT, BASE_MISS)

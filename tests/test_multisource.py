"""多数据源测试：join / union、键对齐、膨胀保护、上传与路径安全。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from rules import RuleValidationError, probe_columns, validate_source_path
from sources import (
    MAX_JOIN_ROWS,
    MAX_UPLOAD_BYTES,
    join_frames,
    read_sources,
    safe_filename,
    save_upload,
    union_frames,
)

ROOT = Path(__file__).resolve().parents[1]
LOTS = ROOT / "lots.csv"


def _left() -> pd.DataFrame:
    return pd.DataFrame({"小区": ["A", "B", "C"], "条数": [3, 2, 1]})


def _right() -> pd.DataFrame:
    return pd.DataFrame({
        "小区名称": [" A ", "B", "D"],
        "片区": ["城东", "城西", "城北"],
    })


# ============================================================
# join / union
# ============================================================
def test_join_aligns_keys_and_reports_match_rate():
    merged, stats = join_frames(
        _left(), _right(), how="left",
        left_on=["小区"], right_on=["小区名称"], suffixes=("", "_维表"),
    )
    assert len(merged) == 3
    assert stats["matched"] == 2
    assert stats["unmatched_left"] == 1
    assert stats["match_rate"] == pytest.approx(66.67, abs=0.01)
    assert "片区" in merged.columns


def test_join_aligns_int_and_str_keys():
    left = pd.DataFrame({"id": [10, 11], "v": [1, 2]})
    right = pd.DataFrame({"code": ["10", " 11 "], "label": ["x", "y"]})
    merged, stats = join_frames(left, right, how="inner",
                                left_on=["id"], right_on=["code"])
    assert len(merged) == 2
    assert set(merged["label"]) == {"x", "y"}
    assert stats["match_rate"] == pytest.approx(100.0)


def test_join_blocks_explosive_right_table():
    left = pd.DataFrame({"k": ["A"] * 5})
    right = pd.DataFrame({"k": ["A"] * 12, "v": range(12)})
    with pytest.raises(RuleValidationError) as exc:
        join_frames(left, right, how="left", left_on=["k"], right_on=["k"])
    assert "重复" in str(exc.value)


def test_join_rejects_oversized_right_table():
    left = pd.DataFrame({"k": ["A"]})
    right = pd.DataFrame({"k": ["A"] * (MAX_JOIN_ROWS + 1)})
    with pytest.raises(RuleValidationError) as exc:
        join_frames(left, right, how="left", left_on=["k"], right_on=["k"])
    assert "上限" in str(exc.value)


def test_union_keeps_common_columns():
    left = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    right = pd.DataFrame({"b": [5], "c": [6]})
    merged, stats = union_frames(left, right)
    assert len(merged) == 3
    assert list(merged.columns) == ["b"]
    assert stats["dropped_columns"] == ["c"]


def test_join_missing_key_reports_clear_error():
    with pytest.raises(RuleValidationError) as exc:
        join_frames(_left(), _right(), how="left",
                    left_on=["不存在"], right_on=["小区名称"])
    assert "关联键不存在" in str(exc.value)


# ============================================================
# 多源读取
# ============================================================
def test_read_sources_skips_main(tmp_path):
    dim = tmp_path / "dim.csv"
    dim.write_text("小区名称,片区\nA,城东\n", encoding="utf-8-sig")
    frames = read_sources({
        "main": {"path": str(LOTS), "format": "csv"},
        "dim": {"path": str(dim), "format": "csv"},
    })
    assert set(frames) == {"dim"}
    assert list(frames["dim"].columns) == ["小区名称", "片区"]


def test_probe_columns_of_multiple_sources(tmp_path):
    dim = tmp_path / "dim.csv"
    dim.write_text("小区名称,片区\nA,城东\n", encoding="utf-8-sig")
    assert probe_columns({"path": str(dim), "format": "csv"}) == ["小区名称", "片区"]


# ============================================================
# 上传安全
# ============================================================
def test_save_upload_writes_under_root(tmp_path):
    info = save_upload("样本 数据.csv", "a,b\n1,2\n".encode("utf-8"), root=tmp_path)
    path = Path(info["path"])
    assert path.exists() and path.parent == tmp_path.resolve()
    assert info["format"] == "csv"
    assert info["size"] == len("a,b\n1,2\n".encode("utf-8"))


def test_save_upload_rejects_bad_extension(tmp_path):
    with pytest.raises(RuleValidationError):
        save_upload("evil.exe", b"MZ", root=tmp_path)


def test_save_upload_rejects_oversized_file(tmp_path):
    with pytest.raises(RuleValidationError) as exc:
        save_upload("big.csv", b"0" * (MAX_UPLOAD_BYTES + 1), root=tmp_path)
    assert "上限" in str(exc.value)


def test_save_upload_sanitizes_path_traversal(tmp_path):
    info = save_upload("../../etc/passwd.csv", b"a\n1\n", root=tmp_path)
    name = Path(info["path"]).name
    assert ".." not in name and "/" not in name and "\\" not in name
    assert Path(info["path"]).parent == tmp_path.resolve()


def test_safe_filename_keeps_chinese_and_limits_length():
    name = safe_filename("房源明细（成交）.csv")
    assert name.endswith(".csv") and "房源明细" in name
    assert len(safe_filename("x" * 400 + ".csv")) <= 80


def test_validate_source_path_blocks_remote_and_escape(tmp_path):
    with pytest.raises(RuleValidationError):
        validate_source_path({"path": "http://evil.example/a.csv"})
    with pytest.raises(RuleValidationError):
        validate_source_path({"path": str(tmp_path / "nope.csv")})
    assert validate_source_path({"path": str(LOTS)}).exists()


# ============================================================
# 端到端：join 任务
# ============================================================
def test_execute_join_task_end_to_end(tmp_path):
    from rule_executor import execute_rule
    from tests.provider_fixture import make_rule

    dim = tmp_path / "communities.csv"
    dim.write_text(
        "小区名称,片区,挂牌均价\nUnknown,城北,15000\n闽乐家园,城西,18000\n",
        encoding="utf-8-sig",
    )
    main = {"path": str(LOTS), "format": "csv"}
    extra = {"path": str(dim), "format": "csv"}
    sources = {"main": main, "dim": extra}
    rule = make_rule(
        intent="按小区关联维表后统计成交套数并排名",
        source_path=str(LOTS),
        sources=sources,
        steps=[
            {"op": "join", "right": "dim", "how": "left",
             "left_on": "小区", "right_on": "小区名称"},
            {"op": "count_rows", "by": "片区", "alias": "条数"},
        ],
    )
    assert rule["steps"][0]["op"] == "join"
    assert rule["steps"][0]["left_on"] == "小区"
    assert rule["steps"][0]["right_on"] == "小区名称"

    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["task"]["task_type"] == "join"
    assert summary["source_count"] == 2
    stats = summary["task"]["join"][0]
    assert stats["how"] == "left"
    assert 0 < stats["match_rate"] <= 100
    assert any(t["name"] == "关联统计" for t in summary["task"]["tables"])

    # 关联后的右表字段可以被后续算子引用（select 保留 小区/片区）
    rule2 = json.loads(json.dumps(rule))
    rule2["steps"] = [
        rule["steps"][0],
        {"op": "select", "columns": ["小区", "片区"]},
        {"op": "head", "n": 5},
    ]
    rule2["output"] = {"formats": ["csv"], "report": False}
    summary2 = execute_rule(rule2, base_output=str(tmp_path))
    result = pd.read_csv(summary2["result_csv"])
    assert list(result.columns) == ["小区", "片区"]
    assert len(result) <= 5

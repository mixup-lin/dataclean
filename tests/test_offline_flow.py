"""离线端到端验证：不依赖 API Key、不依赖网络、不依赖可选第三方库。

覆盖：
  1. 显式规则 JSON 生成链路符合契约（已移除关键词 Mock 模板）
  2. 规则校验拦截非法算子与路径
  3. 用 lots.csv 跑通执行 / 导出 / 报告
  4. 文档抽取 TXT 与图片走显式夹具响应
  5. CLI 旧调用形式回归（-h / --version / 原参数）与 --rule-file 离线执行
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.provider_fixture import FixtureProvider, make_rule

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "lots.csv"
PY = sys.executable


def _provider(rule=None):
    return FixtureProvider(rule=rule or make_rule())


def _cli_env():
    """CLI 子进程隔离本机 .env 与凭据，避免发起真实在线调用。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for key in ("LLM_PROVIDER", "LLM_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    return env


def test_explicit_rule_generation_uses_injected_rule():
    from rules import assert_valid_rule, generate_rules_legacy as generate_rules

    intent = "中位数填充缺失值，剔除常数列，导出 csv"
    cols = ["起拍价(元)", "成交价(元)", "状态"]
    rule = make_rule(
        intent=intent, source_path=str(DATA),
        options={"fill_missing": "median", "drop_constant_cols": True},
        output={"formats": ["csv"], "report": True},
    )
    generated, meta = generate_rules(
        intent, {"path": str(DATA), "format": "csv"}, cols,
        _provider(rule),
    )
    assert meta["mode"] == "offline"
    assert generated["version"] == "2.0"
    assert generated["intent"] == intent
    assert generated["options"]["drop_constant_cols"] is True
    assert generated["steps"] == []
    assert_valid_rule(generated, cols)


def test_rule_validation_rejects_unsafe_input():
    from rules import RuleValidationError, assert_valid_rule, validate_rule

    bad = {
        "version": "2.0", "intent": "x",
        "source": {"path": "http://evil.example/a.csv", "format": "csv"},
        "options": {"fill_missing": "eval('rm -rf')"},
        "steps": [{"op": "__import__", "columns": []}],
        "output": {"formats": ["csv"], "report": True},
    }
    errors = validate_rule(bad)
    assert any("source.path" in e for e in errors)
    assert any("fill_missing" in e for e in errors)
    assert any("op" in e for e in errors)
    with pytest.raises(RuleValidationError):
        assert_valid_rule(bad)


def test_reconcile_drops_unknown_columns():
    from rules import reconcile_rule_columns

    rule = {
        "steps": [{"op": "sort", "by": "不存在的列", "ascending": False}],
        "options": {"group_by": "不存在的列"},
    }
    fixed, warnings = reconcile_rule_columns(rule, ["起拍价(元)", "状态"])
    assert fixed["steps"] == []
    assert fixed["options"]["group_by"] is None
    assert warnings


def test_execute_rule_with_real_csv(tmp_path):
    from rule_executor import execute_rule

    rule = make_rule(
        source_path=str(DATA),
        options={
            "fill_missing": "median", "drop_constant_cols": True,
            "outlier_method": "none", "max_charts": 2,
        },
        steps=[
            {"op": "filter", "column": "起拍价(元)", "operator": "gt", "value": 0},
            {"op": "sort", "by": "起拍价(元)", "ascending": False},
            {"op": "head", "n": 300},
        ],
        output={"formats": ["csv", "json"], "report": True},
    )
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["rows"] == 300
    assert summary["columns"] > 0
    assert summary["metrics"]["质量评分"] >= 0
    names = {a["name"] for a in summary["artifacts"]}
    assert {"result.csv", "result.json", "rule.json", "execution_summary.json"} <= names
    assert any(n.startswith("quality_report_") for n in names)
    assert all(s["status"] == "ok" for s in summary["step_logs"])

    run_dir = tmp_path / summary["run_id"]
    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert len(payload) == 300
    text = (run_dir / "result.json").read_text(encoding="utf-8")
    import re

    assert not re.search(r"(?<![\w\"-])-?\d+(?:\.\d+)?e[+-]\d+", text, re.I)


def test_extract_text_and_image_use_explicit_provider(tmp_path):
    from doc_extract import extract_document, parse_schema
    from exporters import export_extraction, extraction_to_frame, new_run_dir

    doc = tmp_path / "doc.txt"
    doc.write_text(
        "合同编号：HT-TEST-001\n主体名称：测试公司\n金额：1234567\n",
        encoding="utf-8",
    )
    schema = parse_schema("*合同编号:string\n主体名称:string\n金额:number")
    extraction = {
        "fields": {"合同编号": "HT-TEST-001", "主体名称": "测试公司", "金额": "1234567"},
        "tables": [{"name": "table_1", "columns": ["项目", "数量"],
                    "rows": [["A", "2"], ["B", "3"]], "source": "txt"}],
        "confidence": {"合同编号": 0.99},
        "evidence": [{"field": "合同编号", "snippet": "合同编号：HT-TEST-001"}],
    }
    result, meta = extract_document(
        str(doc), schema, FixtureProvider(extraction=extraction)
    )
    assert result["fields"]["合同编号"] == "HT-TEST-001"
    assert result["fields"]["金额"] == "1234567"
    assert result["tables"] and result["tables"][0]["columns"] == ["项目", "数量"]
    assert meta["mode"] == "offline"
    frame = extraction_to_frame(result)
    assert frame is not None and len(frame) == 2
    arts = export_extraction(result, new_run_dir(str(tmp_path)))
    assert {a.name for a in arts} == {"extraction.json", "extraction.csv"}

    # 1x1 PNG（最小合法占位），验证多模态分支可离线走通
    png = tmp_path / "scan.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
        )
    )
    vision = {
        "fields": {"合同编号": "HT-TEST-IMG", "主体名称": "测试公司", "金额": "1"},
        "tables": [], "confidence": {}, "evidence": [],
    }
    vresult, vmeta = extract_document(
        str(png), schema, FixtureProvider(vision=vision)
    )
    assert vresult["source"]["format"] == "png"
    assert vmeta["mode"] == "offline"
    assert "合同编号" in vresult["fields"]


def test_no_credentials_fails_loudly_instead_of_guessing(monkeypatch):
    """移除 Mock 后，无凭据 + 无显式规则必须明确报错，不能悄悄编规则。"""
    import llm_provider
    from rules import generate_rules_legacy as generate_rules

    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm_provider, "load_dotenv", lambda *a, **kw: None)
    with pytest.raises(llm_provider.LLMError) as exc:
        generate_rules(
            "按小区统计条数", {"path": str(DATA), "format": "csv"}, ["小区"],
        )
    assert "LLM_API_KEY" in str(exc.value)
    assert "规则 JSON" in str(exc.value)


def test_legacy_cli_still_works(tmp_path):
    env = _cli_env()

    def run(args):
        return subprocess.run(
            [PY, "etl_pipeline.py"] + args, cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    p = run(["-h"])
    assert p.returncode == 0 and "--max-charts" in p.stdout
    p = run(["--version"])
    assert p.returncode == 0 and "1.0.0" in p.stdout
    p = run([])
    assert p.returncode == 2
    p = run(["lots.csv", "-f", "csv", "-o", str(tmp_path / "legacy")])
    assert p.returncode == 0, p.stdout + p.stderr
    reports = list((tmp_path / "legacy").glob("quality_report_*.html"))
    assert reports and reports[0].stat().st_size > 1000
    html = reports[0].read_text(encoding="utf-8")
    import re

    assert "统计摘要" in html and "清洗前基线" in html
    assert not re.search(r"(?<![\w\"-])-?\d+(?:\.\d+)?e[+-]\d+", html, re.I)


def test_new_cli_rule_file_offline(tmp_path):
    """离线入口改为 --rule-file：无需 LLM、无需 Mock 模板。"""
    env = _cli_env()
    rule_path = tmp_path / "rule.json"
    rule_path.write_text(
        json.dumps(make_rule(source_path="lots.csv", output={"formats": ["csv"], "report": False}),
                   ensure_ascii=False),
        encoding="utf-8",
    )
    p = subprocess.run(
        [PY, "etl_pipeline.py", "nl", "离线规则校验", "--source", "lots.csv",
         "--rule-file", str(rule_path), "--rules-only"],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert '"version": "2.0"' in p.stdout
    assert "Provider: file / offline" in p.stdout


GROUP_INTENT = "按小区分组，统计每个小区有几条数据"


def _group_rule(extra_options=None, with_steps=True):
    steps = [
        {"op": "filter", "column": "成交价(元)", "operator": "gt", "value": 0},
        {"op": "aggregate", "by": "小区", "column": "成交价(元)",
         "func": "count", "alias": "条数"},
    ]
    options = {"fill_missing": "median", "outlier_method": "none"}
    if extra_options:
        options.update(extra_options)
    if not with_steps:
        options.update({"group_by": "小区", "agg_funcs": ["count", "mean", "sum"]})
    return make_rule(
        intent=GROUP_INTENT, source_path=str(DATA),
        options=options, steps=steps if with_steps else [],
    )


def _result_rows(tmp_path, summary):
    import csv

    path = tmp_path / summary["run_id"] / "result.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def test_group_count_intent_offline_flow(tmp_path):
    from rule_executor import execute_rule

    rule = _group_rule()
    ops = [s["op"] for s in rule["steps"]]
    assert "filter" in ops and "aggregate" in ops
    assert rule["steps"][0]["column"] == "成交价(元)"
    assert rule["steps"][-1]["by"] == "小区"
    assert rule["steps"][-1]["func"] == "count"

    summary = execute_rule(rule, base_output=str(tmp_path))
    assert 1000 < summary["rows"] < 2645
    assert summary["columns"] == 2
    assert not summary["safety_warnings"]

    rows = _result_rows(tmp_path, summary)
    assert rows[0] == ["小区", "条数"]
    # 过滤必须基于原始成交价：缺失被填充成中位数时会错误地保留 19954 行
    assert sum(int(r[1]) for r in rows[1:]) == 7702

    html = (tmp_path / summary["run_id"] / summary["report_name"]).read_text(
        encoding="utf-8"
    )
    assert len(html) < 300_000
    assert "处理结果" in html and "条数" in html


def test_drop_fill_safety_and_filter_column_protection(tmp_path):
    from rule_executor import execute_rule

    rule = _group_rule(extra_options={"fill_missing": "drop"})
    summary = execute_rule(rule, base_output=str(tmp_path))
    assert summary["rows"] > 0
    assert summary["safety_warnings"]
    rows = _result_rows(tmp_path, summary)
    assert rows[0] == ["小区", "条数"]
    assert sum(int(r[1]) for r in rows[1:]) == 7702


def test_report_grouped_table_is_bounded(tmp_path):
    from rule_executor import execute_rule

    rule = _group_rule(with_steps=False)
    summary = execute_rule(rule, base_output=str(tmp_path))
    html = (tmp_path / summary["run_id"] / summary["report_name"]).read_text(
        encoding="utf-8"
    )
    assert len(html) < 400_000
    assert "分组统计（最多 200 行）" in html

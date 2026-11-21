# -*- coding: utf-8 -*-
"""规则库：保存 / 列表 / 复用 / 删除 / 路径安全 / Web 与 CLI 接线。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rule_store  # noqa: E402
from tests.provider_fixture import make_rule  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rule_store, "RULES_DIR", tmp_path / "rules")
    return rule_store


def _rule(**kw):
    return make_rule(
        source_path="lots.csv",
        steps=[
            {"op": "filter", "conditions": [
                {"column": "市", "operator": "eq", "value": "福州"},
                {"column": "小区", "operator": "notna"}], "logic": "and"},
            {"op": "date_filter", "column": "结束时间", "year": 2026},
            {"op": "count_rows", "by": "小区", "alias": "条数"},
        ],
        **kw,
    )


def test_save_list_get_delete_roundtrip(store):
    rule = _rule()
    saved = store.save_rule("福州2026小区统计", rule, intent=rule["intent"])
    assert saved.id and saved.op_count == 3
    assert saved.source_path == "lots.csv"
    assert store.list_rules()[0].id == saved.id

    got = store.get_rule(saved.id)
    assert got is not None and got.rule["steps"][1]["year"] == 2026
    assert store.load_rule_dict(saved.name)["steps"] == rule["steps"]

    assert store.delete_rule(saved.id) is True
    assert store.list_rules() == []
    assert store.delete_rule(saved.id) is False


def test_update_keeps_created_at(store):
    first = store.save_rule("规则A", _rule())
    second = store.save_rule("规则A（改）", _rule(), rule_id=first.id)
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert len(store.list_rules()) == 1


def test_name_and_id_validation(store):
    with pytest.raises(rule_store.RuleStoreError):
        store.save_rule("   ", _rule())
    with pytest.raises(rule_store.RuleStoreError):
        store.get_rule("../etc/passwd")
    with pytest.raises(rule_store.RuleStoreError):
        store.load_rule_dict("不存在的规则")


def test_corrupted_file_is_skipped(store):
    store.save_rule("正常规则", _rule())
    bad = store.RULES_DIR / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(store.list_rules()) == 1


def test_web_save_load_execute_delete(tmp_path, monkeypatch):
    import webapp

    monkeypatch.setattr(rule_store, "RULES_DIR", tmp_path / "rules")
    state = webapp.AppState(str(tmp_path / "output"))
    rule = _rule()
    raw = json.dumps(rule, ensure_ascii=False)

    # 保存
    reply = webapp.handle_request("POST", "/rules/save", {}, {
        "rule_json": [raw], "rule_name": ["福州2026小区统计"],
        "intent": [rule["intent"]],
    }, state)
    assert reply.status == 200 and "规则保存失败" not in reply.body
    items = rule_store.list_rules()
    assert len(items) == 1
    rid = items[0].id

    # 工作台渲染出规则库与下拉选项
    page = webapp.handle_request("GET", "/", {}, {}, state)
    assert "规则库" in page.body and rid in page.body
    assert 'name="saved_rule_id"' in page.body

    # 载入编辑
    loaded = webapp.handle_request("GET", "/rules/load", {"id": [rid]}, {}, state)
    assert loaded.status == 200 and "date_filter" in loaded.body

    # /plan 直接选用已保存规则（跳过 LLM）
    planned = webapp.handle_request("POST", "/plan", {}, {
        "intent": ["随便写点什么"],
        "source_path": ["lots.csv"], "source_format": ["csv"],
        "saved_rule_id": [rid], "formats": ["csv"],
    }, state)
    assert planned.status == 200
    assert "校验未通过" not in planned.body and "规则生成失败" not in planned.body
    assert "count_rows" in planned.body

    # 删除
    deleted = webapp.handle_request("POST", "/rules/delete", {}, {
        "rule_id": [rid],
    }, state)
    assert deleted.status == 302
    assert rule_store.list_rules() == []


def test_cli_rules_list_and_nl_rule_flag(tmp_path, monkeypatch):
    import etl_pipeline

    monkeypatch.setattr(rule_store, "RULES_DIR", tmp_path / "rules")
    rule = _rule()
    rule_path = tmp_path / "rule.json"
    rule_path.write_text(json.dumps(rule, ensure_ascii=False), encoding="utf-8")
    assert etl_pipeline._cmd_rules(["save", "CLI规则", str(rule_path)]) == 0
    assert len(rule_store.list_rules()) == 1
    assert etl_pipeline._cmd_rules(["list"]) == 0
    assert etl_pipeline._cmd_rules(["show", "CLI规则"]) == 0

    # nl --rule 直接复用（--rules-only 不执行管道）
    assert etl_pipeline._cmd_nl([
        "占位需求", "--rule", "CLI规则", "--rules-only",
        "-o", str(tmp_path / "output"),
    ]) == 0
    assert etl_pipeline._cmd_rules(["delete", "CLI规则"]) == 0
    assert rule_store.list_rules() == []

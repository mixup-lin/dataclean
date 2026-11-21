# -*- coding: utf-8 -*-
"""界面等待状态 + 文档抽取同步/异步双模式回归。

覆盖：等待层注入、模式选择、后台任务状态机、进度回调、任务页自动刷新、
完成后结果取回、任务列表、状态 JSON 与失败状态可见。
"""
from __future__ import annotations

import json
from pathlib import Path

import doc_extract
import jobs
import webapp
from tests.provider_fixture import FixtureProvider

SCHEMA_TEXT = "*合同编号:string\n主体名称:string\n金额:number"


def _extraction():
    return {
        "fields": {"合同编号": "HT-2026-0088", "主体名称": "甲公司", "金额": "2400000"},
        "tables": [], "confidence": {}, "evidence": [],
    }


def _doc_bytes() -> bytes:
    return (
        "这是无关的开头说明文字。\n" * 6
        + "合同编号：HT-2026-0088\n主体名称：甲公司\n金额：2400000\n"
        + "这是无关的结尾说明文字。\n" * 6
    ).encode("utf-8")


# ============================================================
# 后台任务状态机
# ============================================================
def test_job_manager_success_progress_and_result():
    manager = jobs.JobManager()

    def work(progress):
        progress(0, 2, "准备")
        progress(1, 2, "处理第 1 份")
        progress(2, 2, "完成")
        return {"ok": True}

    jid = manager.submit("unit", "单元测试任务", work, total=2, meta={"mode": "异步"})
    snap = manager.wait(jid, timeout=5)
    assert snap["status"] == "done"
    assert snap["progress"] == 1.0 and snap["current"] == 2
    assert manager.result(jid) == {"ok": True}
    assert snap["meta"]["mode"] == "异步"
    assert snap["elapsed"] >= 0


def test_job_manager_captures_failure():
    manager = jobs.JobManager()

    def broken(progress):
        raise RuntimeError("模拟 LLM 失败")

    jid = manager.submit("unit", "失败任务", broken)
    snap = manager.wait(jid, timeout=5)
    assert snap["status"] == "failed"
    assert "模拟 LLM 失败" in snap["error"]
    assert "result" not in snap  # JSON 快照默认不含大结果对象


def test_batch_extract_reports_progress(tmp_path):
    seen = []
    tmp = tmp_path
    paths = []
    for i in range(3):
        p = tmp / f"d{i}.txt"
        p.write_text("x", encoding="utf-8")
        paths.append(str(p))
    monkeypatch_provider = FixtureProvider(extraction=_extraction())
    # 只验证进度回调，不触发真实 LLM：让 extract_document 走 fixture
    batch = doc_extract.extract_documents_batch(
        paths, doc_extract.parse_schema(SCHEMA_TEXT),
        provider=monkeypatch_provider, max_workers=2,
        on_progress=lambda done, total, name: seen.append((done, total, name)),
    )
    assert batch["meta"]["total"] == 3
    assert seen[0][0] == 0 and seen[-1][0] == 3
    assert all(total == 3 for _, total, _ in seen[1:])


# ============================================================
# 页面：等待层与模式选择
# ============================================================
def test_extract_page_has_wait_layer_and_mode_selector(tmp_path):
    reply = webapp.handle_request(
        "GET", "/extract", {}, {}, webapp.AppState(str(tmp_path))
    )
    body = reply.body
    assert 'id="dc-wait"' in body and "正在处理" in body
    assert 'name="mode" value="sync"' in body
    assert 'name="mode" value="async"' in body
    assert "data-stages" in body and 'data-wait="正在执行结构化抽取…"' in body
    assert "后台任务" in body


def test_workbench_and_result_page_inject_wait_layer(tmp_path):
    reply = webapp.handle_request("GET", "/", {}, {},
                                  webapp.AppState(str(tmp_path)))
    assert 'id="dc-wait"' in reply.body
    assert "data-stages" in reply.body
    assert 'data-wait="正在生成规则并执行…"' in reply.body
    assert 'href="/jobs"' in reply.body


def test_static_css_has_wait_and_progress_styles():
    reply = webapp.handle_request("GET", "/static/style.css", {}, {},
                                  webapp.AppState("./output"))
    assert ".dc-wait.on" in reply.body
    assert ".job-fill" in reply.body
    assert "@keyframes dc-bounce" in reply.body


# ============================================================
# Web 端到端：同步 / 异步
# ============================================================
def _post_extract(state, mode, files=None):
    return webapp.handle_request("POST", "/extract", {}, {
        "schema_text": [SCHEMA_TEXT],
        "mode": [mode],
        "export_formats": ["csv", "xlsx"],
        "workers": ["2"],
    }, state, files=files or {"doc_files": [("d1.txt", _doc_bytes()),
                                            ("d2.txt", _doc_bytes())]})


def test_sync_extract_still_returns_result_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_extract, "DOC_UPLOAD_DIR",
                        tmp_path / "uploads" / "docs")
    monkeypatch.setattr(doc_extract, "get_provider",
                        lambda *a, **kw: FixtureProvider(extraction=_extraction()))
    state = webapp.AppState(str(tmp_path / "output"))
    reply = _post_extract(state, "sync")
    assert reply.status == 200
    assert "批量处理" in reply.body
    assert "extraction_batch.csv" in reply.body
    assert 'name="mode" value="sync" checked' in reply.body


def test_async_extract_submit_poll_and_collect(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_extract, "DOC_UPLOAD_DIR",
                        tmp_path / "uploads" / "docs")
    monkeypatch.setattr(doc_extract, "get_provider",
                        lambda *a, **kw: FixtureProvider(extraction=_extraction()))
    state = webapp.AppState(str(tmp_path / "output"))

    submitted = _post_extract(state, "async")
    assert submitted.status == 302
    location = submitted.headers["Location"]
    assert location.startswith("/job/")
    jid = location.rsplit("/", 1)[-1]

    status_reply = webapp.handle_request(
        "GET", f"/job/{jid}/status", {}, {}, state
    )
    assert status_reply.content_type.startswith("application/json")
    snap = json.loads(status_reply.body)
    assert snap["id"] == jid and snap["kind"] == "extract"
    assert snap["status"] in ("queued", "running", "done")
    assert snap["meta"]["documents"] == 2

    final = webapp.JOBS.wait(jid, timeout=15)
    assert final is not None and final["status"] == "done", final
    assert final["progress"] == 1.0

    result_page = webapp.handle_request("GET", location, {}, {}, state)
    assert result_page.status == 200
    assert "批量处理" in result_page.body
    assert "extraction_batch.csv" in result_page.body
    assert "异步任务已完成" in result_page.body
    assert "抽取失败" not in result_page.body

    listing = webapp.handle_request("GET", "/jobs", {}, {}, state)
    assert listing.status == 200
    assert "文档抽取" in listing.body and jid in listing.body


def test_unknown_job_status_returns_404(tmp_path):
    reply = webapp.handle_request(
        "GET", "/job/does-not-exist/status", {}, {},
        webapp.AppState(str(tmp_path)),
    )
    assert reply.status == 404
    assert "not found" in reply.body


def test_failed_async_job_shows_progress_page_with_error(tmp_path):
    def broken(progress):
        raise RuntimeError("异步任务模拟失败")

    jid = webapp.JOBS.submit("extract", "失败抽取任务", broken, total=1,
                             meta={"mode": "异步", "documents": 1})
    snap = webapp.JOBS.wait(jid, timeout=5)
    assert snap["status"] == "failed"
    reply = webapp.handle_request("GET", f"/job/{jid}", {}, {},
                                  webapp.AppState(str(tmp_path)))
    assert reply.status == 200
    assert "任务失败" in reply.body and "异步任务模拟失败" in reply.body
    assert 'http-equiv="refresh"' not in reply.body

# -*- coding: utf-8 -*-
"""文档抽取：节能模式（语义定位 + 滑动窗口）、批量并发、横向宽表与 Excel 导出。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doc_extract  # noqa: E402
from tests.provider_fixture import FixtureProvider  # noqa: E402

SCHEMA_TEXT = "*合同编号:string\n主体名称:string\n日期:date\n金额:number"


class RecordingProvider(FixtureProvider):
    """捕获送入 LLM 的 user JSON，用于验证节能模式只送片段。"""

    def __init__(self, extraction):
        super().__init__(extraction=extraction)
        self.users: list = []

    def complete(self, system, user):
        self.users.append(user)
        return super().complete(system, user)


def _filler_head() -> str:
    return "这是无关的开头说明文字。\n" * 40


def _filler_tail() -> str:
    # 标记只出现在很靠后的一行：真实文档不会把同一个无关标记铺满每一行，
    # 放在远处才能验证「窗口外的正文没有进入 LLM 输入」。
    return "这是无关的结尾说明文字。\n" * 40 + "FILLER_TAIL_MARKER 仅此一行。\n"


def _doc(path: Path, name: str = "doc.txt") -> str:
    text = (
        _filler_head()
        + "合同编号：HT-2026-0088\n"
        + "主体名称：福州某某置业有限公司\n"
        + "日期：2026-11-08\n"
        + "金额：2400000\n"
        + _filler_tail()
    )
    p = path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _extraction(fields=None):
    return {
        "fields": fields or {
            "合同编号": "HT-2026-0088", "主体名称": "福州某某置业有限公司",
            "日期": "2026-11-08", "金额": "2400000",
        },
        "tables": [], "confidence": {}, "evidence": [],
    }


# ============================================================
# 节能模式核心
# ============================================================
def test_build_field_keywords_expands_semantics():
    schema = doc_extract.parse_schema(SCHEMA_TEXT)
    kws = doc_extract.build_field_keywords(schema)
    assert "合同编号" in kws and "金额" in kws
    # 概念同义词扩展：金额字段应带出「总价 / 价款」
    assert "总价" in kws and "价款" in kws
    # 过短的高频字必须被过滤
    assert "元" not in kws and "号" not in kws


def test_parse_schema_accepts_keyword_segment():
    schema = doc_extract.parse_schema("金额:number:总价|价款\n*名称:string")
    assert schema[0]["keywords"] == ["总价", "价款"]
    assert schema[1]["required"] is True
    assert "总价|价款" in doc_extract.schema_to_text(schema)


def test_locate_snippets_reduces_text_and_keeps_values():
    text = (
        _filler_head()
        + "合同编号：HT-2026-0088\n主体名称：福州某某置业有限公司\n"
        + "日期：2026-11-08\n金额：2400000\n"
        + _filler_tail()
    )
    kws = doc_extract.build_field_keywords(doc_extract.parse_schema(SCHEMA_TEXT))
    cfg = doc_extract.EcoConfig(window=60, max_snippets=10, max_chars=8000)
    focus = doc_extract.locate_snippets(text, kws, cfg)
    assert focus.fallback is False
    assert focus.hits > 0 and focus.snippets > 0
    assert "HT-2026-0088" in focus.text and "2400000" in focus.text
    assert "FILLER_TAIL_MARKER" not in focus.text
    assert focus.sent_chars < focus.original_chars / 2
    assert focus.saved_ratio > 0.5
    assert "[片段" in focus.text


def test_locate_snippets_merges_overlapping_windows():
    text = "金额：100\n总价：200\n价款：300\n"
    kws = ["金额", "总价", "价款"]
    focus = doc_extract.locate_snippets(
        text, kws, doc_extract.EcoConfig(window=40, merge_gap=40)
    )
    assert focus.snippets == 1          # 相邻窗口应合并为一个片段
    assert focus.sent_chars < len(text) + 200


def test_locate_snippets_falls_back_to_full_text():
    text = "全篇没有任何目标字段信息，只有一句完全不相关的叙述。\n" * 20
    focus = doc_extract.locate_snippets(text, ["合同编号", "主体名称", "金额"])
    assert focus.fallback is True
    assert focus.hits == 0
    assert focus.text.startswith("全篇没有任何目标字段信息")
    assert "回退全文" in focus.reason


def test_locate_snippets_respects_char_budget():
    text = ("无关文字。\n" * 5 + "金额：100\n" + "无关文字。\n" * 5) * 20
    focus = doc_extract.locate_snippets(
        text, ["金额"], doc_extract.EcoConfig(window=80, max_chars=300)
    )
    assert focus.sent_chars <= 300


def test_extract_document_eco_sends_only_focused_text(tmp_path):
    path = _doc(tmp_path)
    provider = RecordingProvider(_extraction())
    result, meta = doc_extract.extract_document(
        path, doc_extract.parse_schema(SCHEMA_TEXT), provider=provider,
        eco=True, eco_config={"window": 60, "max_chars": 8000},
    )
    assert result["fields"]["合同编号"] == "HT-2026-0088"
    eco = meta["eco"]
    assert eco["fallback"] is False and eco["saved_ratio"] > 0.5
    user = json.loads(provider.users[0])
    assert user["extraction_mode"] == "focused"
    assert "HT-2026-0088" in user["text"]
    assert "FILLER_TAIL_MARKER" not in user["text"]
    assert "仅依据这些片段" in user["instruction"]


def test_extract_document_full_mode_sends_everything(tmp_path):
    path = _doc(tmp_path)
    provider = RecordingProvider(_extraction())
    _, meta = doc_extract.extract_document(
        path, doc_extract.parse_schema(SCHEMA_TEXT), provider=provider, eco=False,
    )
    assert meta["eco"] is None
    user = json.loads(provider.users[0])
    assert "extraction_mode" not in user
    assert "FILLER_TAIL_MARKER" in user["text"]


def test_eco_fallback_when_no_keyword_hits(tmp_path):
    path = tmp_path / "nohit.txt"
    path.write_text("本文档通篇没有任何目标字段。\n" * 30, encoding="utf-8")
    provider = RecordingProvider(_extraction())
    _, meta = doc_extract.extract_document(
        str(path), [{"name": "完全不存在的字段", "type": "string"}],
        provider=provider, eco=True,
    )
    assert meta["eco"]["fallback"] is True
    user = json.loads(provider.users[0])
    assert "extraction_mode" not in user
    assert "本文档通篇" in user["text"]


# ============================================================
# 批量抽取（性能与容错）
# ============================================================
def test_batch_concurrent_and_isolates_failures(tmp_path):
    paths = [_doc(tmp_path, f"d{i}.txt") for i in range(3)]
    paths.append(str(tmp_path / "missing.txt"))     # 单份失败
    provider = RecordingProvider(_extraction())
    batch = doc_extract.extract_documents_batch(
        paths, doc_extract.parse_schema(SCHEMA_TEXT), provider=provider,
        eco=True, eco_config={"window": 60}, max_workers=3,
    )
    meta = batch["meta"]
    assert meta["total"] == 4 and meta["succeeded"] == 3 and meta["failed"] == 1
    assert meta["workers"] == 3
    assert meta["eco"] is True and meta["eco_saved_ratio"] > 0
    assert len(batch["results"]) == 3
    assert batch["errors"][0]["name"] == "missing.txt"
    assert len(provider.users) == 3                 # 成功份各调用一次 LLM
    assert all("FILLER_TAIL_MARKER" not in u for u in provider.users)


def test_batch_rejects_empty_paths():
    with pytest.raises(doc_extract.ExtractionError):
        doc_extract.extract_documents_batch([], provider=FixtureProvider())


# ============================================================
# 横向宽表 / Excel / 批量导出
# ============================================================
def test_extraction_frame_is_wide_with_field_names_as_header():
    from exporters import extraction_to_frame

    frame = extraction_to_frame({"fields": {"合同编号": "HT-1", "金额": "100"}, "tables": []})
    assert list(frame.columns) == ["合同编号", "金额"]     # 第一行即字段名
    assert len(frame) == 1
    assert frame.iloc[0]["合同编号"] == "HT-1"
    legacy = extraction_to_frame(
        {"fields": {"合同编号": "HT-1", "金额": "100"}, "tables": []}, wide=False
    )
    assert list(legacy.columns) == ["字段", "值"]


def test_export_extraction_writes_csv_and_xlsx(tmp_path):
    from exporters import export_extraction

    result = {"source": {"path": "x.txt"}, "fields": {"合同编号": "HT-1", "金额": "100"},
              "tables": [], "confidence": {}, "evidence": []}
    arts = export_extraction(result, tmp_path, formats=("csv", "xlsx"))
    names = {a.name for a in arts}
    assert {"extraction.json", "extraction.csv", "extraction.xlsx"} <= names
    csv = pd.read_csv(tmp_path / "extraction.csv", encoding="utf-8-sig")
    xlsx = pd.read_excel(tmp_path / "extraction.xlsx", engine="openpyxl")
    assert list(csv.columns) == ["合同编号", "金额"]
    assert list(xlsx.columns) == ["合同编号", "金额"]
    assert str(csv.iloc[0]["合同编号"]) == "HT-1"


def test_export_batch_wide_table_one_row_per_document(tmp_path):
    from exporters import batch_to_frame, export_extraction_batch

    batch = {
        "schema": [{"name": "合同编号"}, {"name": "金额"}],
        "meta": {"total": 2, "succeeded": 1, "failed": 1},
        "documents": [
            {"path": "a.txt", "name": "a.txt", "status": "ok", "error": "",
             "result": {"fields": {"合同编号": "HT-1", "金额": "100"}}, "meta": {}},
            {"path": "b.txt", "name": "b.txt", "status": "error",
             "error": "解析失败", "result": None, "meta": {}},
        ],
        "errors": [],
    }
    frame = batch_to_frame(batch)
    assert list(frame.columns) == ["合同编号", "金额", "_源文件", "_状态", "_错误"]
    assert len(frame) == 2
    assert frame.iloc[1]["_状态"] == "error"

    arts = export_extraction_batch(batch, tmp_path, formats=("csv", "xlsx"))
    names = {a.name for a in arts}
    assert {"extraction_batch.json", "extraction_batch.csv",
            "extraction_batch.xlsx"} <= names
    csv = pd.read_csv(tmp_path / "extraction_batch.csv", encoding="utf-8-sig")
    assert list(csv.columns)[:2] == ["合同编号", "金额"]    # 字段名横向在第一行
    assert len(csv) == 2


def test_save_document_upload_and_reject_bad_type(tmp_path):
    good = doc_extract.save_document_upload(
        "报告 A.txt", "内容".encode("utf-8"), root=tmp_path / "docs"
    )
    assert Path(good).is_file() and Path(good).suffix == ".txt"
    assert "报告_A" in Path(good).name
    with pytest.raises(doc_extract.ExtractionError):
        doc_extract.save_document_upload("x.exe", b"MZ", root=tmp_path / "docs")
    with pytest.raises(doc_extract.ExtractionError):
        doc_extract.save_document_upload("x.txt", b"", root=tmp_path / "docs")


# ============================================================
# Web / CLI 接线
# ============================================================
def test_extract_page_has_file_picker_eco_and_export_controls(tmp_path):
    import webapp

    reply = webapp.handle_request(
        "GET", "/extract", {}, {}, webapp.AppState(str(tmp_path))
    )
    body = reply.body
    assert 'name="doc_files"' in body and "multiple" in body
    assert 'name="eco"' in body and 'name="eco_window"' in body
    assert 'name="export_formats"' in body
    assert 'name="local_doc"' in body
    assert "滑动窗口" in body


def test_web_batch_extract_with_eco_and_excel(tmp_path, monkeypatch):
    import webapp

    monkeypatch.setattr(doc_extract, "DOC_UPLOAD_DIR", tmp_path / "uploads" / "docs")
    monkeypatch.setattr(doc_extract, "get_provider",
                        lambda *a, **kw: FixtureProvider(extraction=_extraction()))
    state = webapp.AppState(str(tmp_path / "output"))
    content = (
        _filler_head()
        + "合同编号：HT-2026-0088\n金额：2400000\n"
        + _filler_tail()
    ).encode("utf-8")
    files = {"doc_files": [("d1.txt", content), ("d2.txt", content)]}
    reply = webapp.handle_request("POST", "/extract", {}, {
        "schema_text": [SCHEMA_TEXT],
        "eco": ["1"], "eco_window": ["60"],
        "export_formats": ["csv", "xlsx"], "workers": ["2"],
    }, state, files=files)
    assert reply.status == 200
    assert "抽取失败" not in reply.body
    assert "批量处理" in reply.body
    assert "节能模式" in reply.body
    assert "extraction_batch.csv" in reply.body
    assert "extraction_batch.xlsx" in reply.body


def test_cli_extract_batch_eco(tmp_path, monkeypatch):
    import etl_pipeline

    monkeypatch.setattr(doc_extract, "get_provider",
                        lambda *a, **kw: FixtureProvider(extraction=_extraction()))
    d1 = _doc(tmp_path, "c1.txt")
    d2 = _doc(tmp_path, "c2.txt")
    assert etl_pipeline._cmd_extract([
        d1, d2, "--eco", "--eco-window", "60", "--workers", "2",
        "--schema", SCHEMA_TEXT.replace("\n", ";"),
        "-o", str(tmp_path / "output"), "--formats", "csv,xlsx",
    ]) == 0
    produced = list((tmp_path / "output").rglob("extraction_batch.*"))
    assert {p.suffix for p in produced} >= {".csv", ".xlsx", ".json"}

"""标准库降级路径的 multipart/form-data 解析回归测试。

重点覆盖曾导致「需求描述不能为空」的两类问题：
1. 第一个字段（intent）被切分逻辑丢掉；
2. 空值字段（如 columns / 附加表别名）被误删。
"""
from __future__ import annotations

import webapp

BOUNDARY = "----WebKitFormBoundaryTest123"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def _body(nl: bytes = b"\r\n") -> bytes:
    sep = b"--" + BOUNDARY.encode()
    parts = [
        (
            b'Content-Disposition: form-data; name="intent"' + nl + nl
            + "各小区成交套数占比".encode("utf-8") + nl
        ),
        (
            b'Content-Disposition: form-data; name="source_path"' + nl + nl
            + b"lots.csv" + nl
        ),
        (b'Content-Disposition: form-data; name="columns"' + nl + nl + nl),
        (b'Content-Disposition: form-data; name="extra1_alias"' + nl + nl + nl),
        (
            b'Content-Disposition: form-data; name="source_file"; filename=""'
            + nl + b"Content-Type: application/octet-stream" + nl + nl + nl
        ),
        (
            b'Content-Disposition: form-data; name="formats"' + nl + nl
            + b"csv" + nl
        ),
    ]
    out = b""
    for p in parts:
        out += sep + nl + p
    out += sep + b"--" + nl
    return out


def test_intent_first_field_is_parsed():
    form, _ = webapp._parse_multipart(CONTENT_TYPE, _body())
    assert form["intent"] == ["各小区成交套数占比"]
    assert form["source_path"] == ["lots.csv"]
    assert form["formats"] == ["csv"]


def test_empty_fields_are_kept():
    form, _ = webapp._parse_multipart(CONTENT_TYPE, _body())
    assert form.get("columns") == [""]
    assert form.get("extra1_alias") == [""]


def test_empty_filename_is_treated_as_text_field():
    form, files = webapp._parse_multipart(CONTENT_TYPE, _body())
    assert form.get("source_file") == [""]
    assert "source_file" not in files


def test_lf_line_endings_are_supported():
    form, _ = webapp._parse_multipart(CONTENT_TYPE, _body(b"\n"))
    assert form["intent"] == ["各小区成交套数占比"]
    assert form.get("columns") == [""]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档结构化数据抽取。

链路：文档输入 → 解析器统一中间结构 → LLM 抽取（图片走多模态）→ 结构化输出

输入
----
- ``path``   : PDF / DOCX / TXT / MD / CSV / JSON / 图片（PNG/JPG/WEBP/BMP/TIFF）
- ``schema`` : 可选字段定义列表，形如 ``[{"name":"标的号","type":"string","required":true}]``

中间结构 ``ParsedDocument``
--------------------------
- ``text``   : 归一化后的纯文本（按页拼接，带 ``[第N页]`` 标记）
- ``pages``  : 逐页文本列表
- ``images`` : ``(mime_type, bytes)`` 列表（图片输入或扫描件）
- ``meta``   : 格式、页数、字符数、截断标记

输出 ``Dict``（契约）
--------------------
{
  "version": "2.0",
  "source": {"path", "format", "pages", "chars"},
  "schema": [...],
  "fields": {字段名: 值},
  "tables": [{"name", "columns", "rows", "source"}],
  "confidence": {字段名: 0~1},
  "evidence": [{"field", "snippet", "page"}],
  "provider": "...", "mode": "online",
  "generated_by": "...", "warnings": [...]
}
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as dc_field
from threading import Lock
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from llm_provider import (
    LLMError,
    LLMProvider,
    describe_provider,
    get_provider,
    parse_json_response,
)

LOGGER = logging.getLogger("etl")

try:
    import pypdf  # type: ignore
except ImportError:  # pragma: no cover
    pypdf = None  # type: ignore

try:
    import docx  # type: ignore  # python-docx
except ImportError:  # pragma: no cover
    docx = None  # type: ignore


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".tsv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}
SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS | IMAGE_EXTENSIONS | PDF_EXTENSIONS | DOCX_EXTENSIONS
)

MAX_TEXT_CHARS = 30000          # 送入 LLM 的文本上限，避免超长上下文
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_DOC_UPLOAD_BYTES = 30 * 1024 * 1024
DOC_UPLOAD_DIR = Path(__file__).resolve().parent / "uploads" / "docs"
_SAFE_DOC_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fa5._-]+")

# 节能模式默认值：前后窗口字符数、片段数/总量上限、合并间距
ECO_DEFAULTS = {
    "window": 200,
    "max_snippets": 24,
    "max_chars": 12000,
    "merge_gap": 40,
    "max_workers": 4,
}

DEFAULT_SCHEMA: List[Dict[str, Any]] = [
    {"name": "文档标题", "type": "string", "required": False},
    {"name": "主体名称", "type": "string", "required": False},
    {"name": "日期", "type": "string", "required": False},
    {"name": "金额", "type": "number", "required": False},
]


class ExtractionError(RuntimeError):
    """文档解析或抽取失败（已归一化，附用户可读的修复建议）。"""


@dataclass
class ParsedDocument:
    """解析器统一中间结构。"""

    path: str
    format: str
    text: str = ""
    pages: List[str] = dc_field(default_factory=list)
    images: List[Tuple[str, bytes]] = dc_field(default_factory=list)
    meta: Dict[str, Any] = dc_field(default_factory=dict)


# ============================================================
# 解析层
# ============================================================
def _mime(path: Path) -> str:
    guessed = mimetypes.guess_type(str(path))[0]
    if guessed:
        return guessed
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    }.get(path.suffix.lower(), "application/octet-stream")


def _read_text(path: Path, encoding: str = "utf-8") -> str:
    for enc in (encoding, "utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ExtractionError(f"文本编码无法识别：{path.name}")


def parse_document(path: str, encoding: str = "utf-8") -> ParsedDocument:
    """按扩展名分派解析，产出统一中间结构。可选依赖缺失时给出安装提示。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise ExtractionError(f"文档不存在或不是文件：{p}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(
            f"暂不支持 {ext or '(无扩展名)'}，支持："
            + ", ".join(sorted(SUPPORTED_EXTENSIONS))
        )

    if ext in TEXT_EXTENSIONS:
        text = _read_text(p, encoding)
        return ParsedDocument(
            path=str(p), format=ext.lstrip("."), text=text, pages=[text],
            meta={"chars": len(text), "pages": 1},
        )

    if ext in IMAGE_EXTENSIONS:
        blob = p.read_bytes()
        if len(blob) > MAX_IMAGE_BYTES:
            raise ExtractionError(
                f"图片过大（{len(blob) // 1024 // 1024}MB），请压缩到 12MB 以内"
            )
        return ParsedDocument(
            path=str(p), format=ext.lstrip("."), images=[(_mime(p), blob)],
            pages=[],
            meta={"chars": 0, "pages": 1, "image_bytes": len(blob)},
        )

    if ext in PDF_EXTENSIONS:
        if pypdf is None:
            raise ExtractionError(
                "缺少 pypdf，无法解析 PDF。请执行 pip install pypdf，或改用 TXT / 图片输入"
            )
        try:
            reader = pypdf.PdfReader(str(p))
            pages = [(pg.extract_text() or "") for pg in reader.pages]
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"PDF 解析失败：{type(exc).__name__}") from exc
        marked = [f"[第{i + 1}页]\n{body}" for i, body in enumerate(pages)]
        return ParsedDocument(
            path=str(p), format="pdf", text="\n".join(marked), pages=pages,
            images=[], meta={"chars": len("\n".join(marked)), "pages": len(pages)},
        )

    # DOCX
    if docx is None:
        raise ExtractionError(
            "缺少 python-docx，无法解析 DOCX。请执行 pip install python-docx，或另存为 TXT"
        )
    try:
        document = docx.Document(str(p))
        blocks = [para.text for para in document.paragraphs if para.text.strip()]
        tables: List[Dict[str, Any]] = []
        for ti, table in enumerate(document.tables):
            grid = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if grid:
                tables.append(
                    {"name": f"docx_table_{ti + 1}", "columns": grid[0], "rows": grid[1:]}
                )
                blocks.append("| " + " | ".join(grid[0]) + " |")
                for row in grid[1:]:
                    blocks.append("| " + " | ".join(row) + " |")
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"DOCX 解析失败：{type(exc).__name__}") from exc
    text = "\n".join(blocks)
    parsed = ParsedDocument(
        path=str(p), format="docx", text=text, pages=[text],
        meta={"chars": len(text), "pages": 1, "docx_tables": tables},
    )
    return parsed


# ============================================================
# 节能模式：语义定位 + 滑动窗口（本地完成，不调用 LLM）
# ============================================================
# 常见字段概念 → 原文中可能出现的写法。仅用于「定位候选位置」，
# 不参与任何取值判断；真正的结构化抽取仍由 LLM 在片段上完成。
_SEMANTIC_ALIASES: Dict[str, Tuple[str, ...]] = {
    "金额": ("金额", "总价", "价款", "合同金额", "成交价", "评估价", "起拍价", "人民币", "价款总额"),
    "编号": ("编号", "合同编号", "标的号", "证号", "权证号", "流水号", "单据号"),
    "名称": ("名称", "主体名称", "公司名称", "甲方", "乙方", "当事人", "买受人", "处置法院"),
    "日期": ("日期", "签订日期", "签署日期", "签订时间", "结束时间", "开始时间", "登记时间"),
    "地址": ("地址", "所在地", "坐落", "位置", "住所"),
    "面积": ("面积", "建筑面积", "土地面积", "平方米"),
    "数量": ("数量", "套数", "户数", "件数", "人数"),
    "状态": ("状态", "成交状态", "详情状态", "展示状态"),
    "电话": ("电话", "联系电话", "手机", "联系方式"),
    "证件": ("身份证", "证件号", "统一社会信用代码"),
}


@dataclass
class EcoConfig:
    """节能模式参数。``window`` 为命中位置前/后各取的字符数。"""

    window: int = ECO_DEFAULTS["window"]
    max_snippets: int = ECO_DEFAULTS["max_snippets"]
    max_chars: int = ECO_DEFAULTS["max_chars"]
    merge_gap: int = ECO_DEFAULTS["merge_gap"]
    max_workers: int = ECO_DEFAULTS["max_workers"]

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "EcoConfig":
        base = cls()
        if not isinstance(raw, dict):
            return base
        out = cls()
        for key in ("window", "max_snippets", "max_chars", "merge_gap", "max_workers"):
            val = raw.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                setattr(out, key, int(val))
        out.window = min(max(out.window, 20), 2000)
        out.max_snippets = min(max(out.max_snippets, 1), 200)
        out.max_chars = min(max(out.max_chars, 200), MAX_TEXT_CHARS)
        out.max_workers = min(max(out.max_workers, 1), 8)
        return out

    def to_dict(self) -> Dict[str, int]:
        return {
            "window": self.window, "max_snippets": self.max_snippets,
            "max_chars": self.max_chars, "merge_gap": self.merge_gap,
            "max_workers": self.max_workers,
        }


@dataclass
class FocusResult:
    """一次语义定位的结果（含 token 缩减统计）。"""

    text: str                       # 实际送入 LLM 的文本
    fallback: bool = False          # True = 未定位到，退回全文
    original_chars: int = 0
    sent_chars: int = 0
    hits: int = 0                   # 关键词命中次数
    snippets: int = 0               # 合并后送入的片段数
    keywords: List[str] = dc_field(default_factory=list)
    reason: str = ""

    @property
    def saved_ratio(self) -> float:
        if not self.original_chars:
            return 0.0
        return round(max(0.0, 1 - self.sent_chars / self.original_chars), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fallback": self.fallback, "original_chars": self.original_chars,
            "sent_chars": self.sent_chars, "hits": self.hits,
            "snippets": self.snippets, "keywords": self.keywords,
            "saved_ratio": self.saved_ratio, "reason": self.reason,
        }


def build_field_keywords(schema: Sequence[Dict[str, Any]]) -> List[str]:
    """按待抽取字段的语义生成定位关键词（纯本地，不调用 LLM）。

    来源优先级：字段名本身 > schema 显式 ``keywords``/``aliases`` >
    内置概念同义词。短词（中文 1 字 / 英文 <3 字符）会被过滤，避免
    「元」「号」这类高频字把全文都当成命中位置。
    """
    result: List[str] = []

    def add(word: Any) -> None:
        w = str(word or "").strip()
        if not w or (len(w) < 2 and not w.isascii()) or (w.isascii() and len(w) < 3):
            return
        if w not in result:
            result.append(w)

    for item in schema or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        add(name)
        add(name.rstrip("：:"))
        for key in ("keywords", "aliases"):
            val = item.get(key)
            if isinstance(val, (list, tuple)):
                for v in val:
                    add(v)
            elif isinstance(val, str):
                for v in re.split(r"[|,，、;；\s]+", val):
                    add(v)
        for concept, aliases in _SEMANTIC_ALIASES.items():
            if concept in name or any(a in name for a in aliases[:3]):
                for a in aliases:
                    add(a)
    return result[:80]


def locate_snippets(
    text: str, keywords: Sequence[str], cfg: Optional[EcoConfig] = None
) -> FocusResult:
    """在原文中定位关键词，按滑动窗口取片段并合并重叠，产出送给 LLM 的聚焦文本。

    - 每个关键词最多取 8 个命中点，且间距小于半个窗口的重复命中会去重；
    - 窗口向两侧扩展到整行边界，避免把词句截成半截；
    - 相邻片段间隔 <= merge_gap 时合并，避免重复 token；
    - 片段数与总字符数双重限量；
    - **一个关键词都没命中时回退全文**（截断到 MAX_TEXT_CHARS），保证不漏抽。
    """
    cfg = cfg or EcoConfig()
    original = len(text)
    if not text.strip() or not keywords:
        return FocusResult(
            text=text[:MAX_TEXT_CHARS], fallback=True,
            original_chars=original, sent_chars=len(text[:MAX_TEXT_CHARS]),
            keywords=list(keywords), reason="无可定位文本或关键词",
        )

    spans: List[Tuple[int, int, List[str]]] = []
    seen: List[int] = []
    hits = 0
    for kw in keywords:
        if not kw:
            continue
        found = 0
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            if found >= 8:
                break
            pos = m.start()
            if any(abs(pos - prev) < max(20, cfg.window // 2) for prev in seen):
                continue
            seen.append(pos)
            start = max(0, pos - cfg.window)
            end = min(original, m.end() + cfg.window)
            # 裁掉首尾的半行（不向外扩展整行）：既保证片段可读，又避免把
            # 窗口边界外的整行无关内容一起送进 LLM，减少 token 浪费。
            if start > 0 and text[start - 1] != "\n":
                nl = text.find("\n", start)
                start = nl + 1 if nl != -1 else original
            if end < original and text[end - 1] != "\n":
                nl = text.rfind("\n", 0, end)
                end = nl + 1 if nl != -1 else 0
            if end > start:
                spans.append((start, end, [kw]))
            found += 1
            hits += 1

    if not spans:
        clipped = text[:MAX_TEXT_CHARS]
        return FocusResult(
            text=clipped, fallback=True, original_chars=original,
            sent_chars=len(clipped), keywords=list(keywords),
            reason="所有字段关键词均未命中，已回退全文",
        )

    spans.sort(key=lambda x: (x[0], x[1]))
    merged: List[List[Any]] = []
    for start, end, kws in spans:
        if merged and start - merged[-1][1] <= cfg.merge_gap:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2].extend(kws)
        else:
            merged.append([start, end, list(kws)])
    merged = merged[: cfg.max_snippets]

    pieces: List[str] = []
    total = 0
    budget = cfg.max_chars
    for idx, (start, end, _kws) in enumerate(merged, 1):
        prefix = "\n\n" if pieces else ""
        room = budget - total - len(prefix)
        if room <= 0:
            break
        piece = f"{prefix}[片段{idx} · 原文 {start}-{end}]\n{text[start:end]}"
        if len(piece) > budget - total:
            # 预算不够放整个片段时，截断到剩余额度，而不是丢弃整个片段；
            # 这样「已定位到相关内容」时绝不会退化成送全文。
            piece = piece[: budget - total]
        pieces.append(piece)
        total += len(piece)
    focus_text = "".join(pieces)
    if not focus_text:
        clipped = text[:MAX_TEXT_CHARS]
        return FocusResult(
            text=clipped, fallback=True, original_chars=original,
            sent_chars=len(clipped), hits=hits, keywords=list(keywords),
            reason="窗口片段为空，已回退全文",
        )
    return FocusResult(
        text=focus_text, fallback=False, original_chars=original,
        sent_chars=len(focus_text), hits=hits, snippets=len(pieces),
        keywords=list(keywords),
    )


# ------------------------------------------------------------
# 文档上传 / 本地文件枚举（供 Web 文件选择器与批量处理）
# ------------------------------------------------------------
def save_document_upload(
    filename: str, data: bytes, *, root: Optional[Path] = None
) -> str:
    """把上传文档落盘到 uploads/docs/，返回绝对路径字符串。

    与数据源上传分开：这里只接受 ``SUPPORTED_EXTENSIONS`` 里的文档类型，
    文件名清洗 + 随机前缀，最终路径严格限制在目标目录内。
    """
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(
            f"不支持的文档类型 {ext or '(无扩展名)'}，支持："
            + ", ".join(sorted(SUPPORTED_EXTENSIONS))
        )
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ExtractionError(f"上传文档为空：{filename}")
    if len(data) > MAX_DOC_UPLOAD_BYTES:
        raise ExtractionError(
            f"文档 {filename} 超过 {MAX_DOC_UPLOAD_BYTES // (1024 * 1024)}MB 上限"
        )
    base = Path(root) if root else DOC_UPLOAD_DIR
    base.mkdir(parents=True, exist_ok=True)
    name = _SAFE_DOC_NAME.sub("_", Path(str(filename)).name).strip("._") or f"doc{ext}"
    target = (base / f"{uuid.uuid4().hex[:8]}_{name}").resolve()
    if base.resolve() not in target.parents:
        raise ExtractionError(f"非法上传路径：{filename}")
    target.write_bytes(bytes(data))
    LOGGER.info("上传文档已保存: %s (%d 字节)", target.name, len(data))
    return str(target)


def list_local_documents(root: Optional[Path] = None, limit: int = 60) -> List[str]:
    """枚举服务器本地可选文档（项目根 / samples / uploads/docs），供下拉选择。

    只返回相对或绝对存在的支持类型文件，跳过隐藏目录与产物目录，避免把
    几十万行的 HTML 报告混进选择列表。
    """
    base = Path(root) if root else Path(__file__).resolve().parent
    skip = {"output", "__pycache__", ".git", ".workbuddy", "rules", "uploads"}
    out: List[str] = []
    candidates: List[Path] = [base]
    for sub in ("samples", Path("uploads") / "docs"):
        d = base / sub
        if d.exists():
            candidates.append(d)
    for folder in candidates:
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for p in entries:
            if (p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
                    and not p.name.startswith(".")):
                try:
                    out.append(str(p.relative_to(base)))
                except ValueError:
                    out.append(str(p))
        if len(out) >= limit:
            break
    return out[:limit]


# ============================================================
# 抽取层
# ============================================================
_EXTRACT_SYSTEM = """
你是文档结构化抽取器。仅输出一个 JSON 对象，不要解释、不要代码围栏。
输出契约：
{
  "fields": {"<字段名>": "<值>"},
  "tables": [{"name":"table_1","columns":["列1","列2"],"rows":[["值1","值2"]]}],
  "confidence": {"<字段名>": 0.0~1.0},
  "evidence": [{"field":"<字段名>","snippet":"<原文片段>","page":1}]
}
要求：
1) 只抽取文档中确实出现的信息，不要编造；找不到时字段值置为空字符串。
2) 若用户提供了字段定义（schema），字段名必须严格取自该定义。
3) 表格用 columns + rows 二维数组表示；数字保持原样字符串，不要科学计数法。
4) evidence 的 snippet 必须来自原文且不超过 120 字。
5) 图片 / 扫描件按 OCR 结果抽取，并在 confidence 中体现识别把握。
"""


def _build_user(
    parsed: ParsedDocument,
    schema: List[Dict[str, Any]],
    focus: Optional[FocusResult] = None,
) -> str:
    """构造送给 LLM 的 user JSON。

    节能模式下 ``text`` 只含语义定位后的窗口片段，并显式告知模型这是
    「原文片段」而非全文，避免模型把片段标记当成内容或臆造缺失字段。
    """
    if focus is not None and not focus.fallback:
        text = focus.text
        truncated = False
    else:
        text = parsed.text
        truncated = len(text) > MAX_TEXT_CHARS
        if truncated:
            text = text[:MAX_TEXT_CHARS] + "\n...[文本已截断]"
    payload: Dict[str, Any] = {
        "schema": schema,
        "text": text,
        "pages": parsed.meta.get("pages", 1),
        "truncated": truncated,
        "instruction": "按契约抽取结构化数据，仅输出 JSON。",
    }
    if focus is not None and not focus.fallback:
        payload["extraction_mode"] = "focused"
        payload["instruction"] = (
            "以下 text 是从原文中按字段语义定位后截取的窗口片段（[片段N · 原文 起-止] 为位置标记，"
            "不是正文内容）。请仅依据这些片段抽取 schema 字段："
            "片段中确实出现的信息才填入，找不到的字段置为空字符串，禁止编造。"
            "evidence.snippet 必须是片段里的原文连续子串，不超过 120 字。"
            "按契约输出 JSON。"
        )
    return json.dumps(payload, ensure_ascii=False)


def _normalize_result(
    raw: Dict[str, Any],
    parsed: ParsedDocument,
    schema: List[Dict[str, Any]],
    info: Any,
    warnings: List[str],
) -> Dict[str, Any]:
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    tables = raw.get("tables") if isinstance(raw.get("tables"), list) else []
    conf = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []

    clean_tables: List[Dict[str, Any]] = []
    for i, t in enumerate(tables):
        if not isinstance(t, dict):
            continue
        cols = [str(c) for c in (t.get("columns") or [])]
        rows = [list(r) if isinstance(r, (list, tuple)) else [r]
                for r in (t.get("rows") or [])]
        if cols and rows:
            width = len(cols)
            rows = [
                [(str(v) if v is not None else "") for v in r[:width]]
                + [""] * max(0, width - len(r))
                for r in rows
            ]
            clean_tables.append(
                {
                    "name": str(t.get("name") or f"table_{i + 1}"),
                    "columns": cols,
                    "rows": rows,
                    "source": str(t.get("source") or parsed.format),
                }
            )

    if schema:
        allowed = {str(s.get("name")) for s in schema}
        unknown = [k for k in fields if k not in allowed]
        if unknown:
            warnings.append(f"已忽略 schema 之外的字段：{unknown}")
        fields = {k: fields.get(k, "") for k in allowed}

    return {
        "version": "2.0",
        "source": {
            "path": parsed.path,
            "format": parsed.format,
            "pages": parsed.meta.get("pages", 1),
            "chars": parsed.meta.get("chars", 0),
        },
        "schema": schema,
        "fields": {str(k): str(v) if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)
                   for k, v in fields.items()},
        "tables": clean_tables,
        "confidence": {str(k): float(v) for k, v in conf.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)},
        "evidence": [
            {
                "field": str(e.get("field", "")),
                "snippet": str(e.get("snippet", ""))[:120],
                "page": e.get("page", 1),
            }
            for e in evidence if isinstance(e, dict)
        ][:50],
        "provider": info.name,
        "mode": info.mode,
        "generated_by": getattr(info, "model", ""),
        "warnings": warnings,
    }


def extract_document(
    path: str,
    schema: Optional[List[Dict[str, Any]]] = None,
    provider: Optional[LLMProvider] = None,
    encoding: str = "utf-8",
    eco: bool = False,
    eco_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """执行「文档抽取 → 结构化输出」，返回 (result, meta)。

    ``eco=True`` 时启用节能模式：先按 schema 字段语义在本地文本中定位候选
    位置，取前后 N 字符的滑动窗口片段合并后交给 LLM；若完全定位不到则
    自动回退全文，保证不漏抽。图片 / 扫描件没有可定位文本，自动走全文分支。
    """
    started = time.monotonic()
    schema = schema or list(DEFAULT_SCHEMA)
    parsed = parse_document(path, encoding)
    info = describe_provider(provider or get_provider())
    p = provider or get_provider()
    warnings: List[str] = []
    if parsed.meta.get("chars", 0) > MAX_TEXT_CHARS:
        warnings.append("文档超长，已截断送入模型，建议分页抽取")

    # DOCX 内嵌表格直接作为结构化候选，无需 LLM 猜测
    embedded_tables = parsed.meta.get("docx_tables") or []

    focus: Optional[FocusResult] = None
    if eco:
        cfg = EcoConfig.from_dict(eco_config)
        if parsed.text.strip():
            keywords = build_field_keywords(schema)
            focus = locate_snippets(parsed.text, keywords, cfg)
            if (not focus.fallback
                    and focus.sent_chars >= focus.original_chars):
                # 短文档里「片段标记 + 窗口」可能比原文还长：节能模式必须
                # 真的省 token 才有意义，此时直接改用全文（等价且更便宜）。
                focus.fallback = True
                focus.text = parsed.text[:MAX_TEXT_CHARS]
                focus.sent_chars = len(focus.text)
                focus.snippets = 0
                focus.reason = "文档较短，定位片段不比全文更省，直接使用全文"
                warnings.append(focus.reason)
            if focus.fallback:
                warnings.append(f"节能模式使用全文（{focus.reason}）")
            else:
                LOGGER.info(
                    "节能模式定位成功 file=%s 片段=%d 命中=%d 字符 %d→%d (省 %.1f%%)",
                    Path(path).name, focus.snippets, focus.hits,
                    focus.original_chars, focus.sent_chars, focus.saved_ratio * 100,
                )
        else:
            focus = None
            warnings.append("图片/扫描件无可定位文本，节能模式自动按全文/多模态处理")

    user = _build_user(parsed, schema, focus)
    meta: Dict[str, Any] = {
        "provider": info.name, "mode": info.mode, "model": info.model,
        "reason": info.reason, "degraded": False,
        "eco": focus.to_dict() if focus is not None else None,
    }
    raw: Dict[str, Any] = {}
    try:
        if parsed.images:
            # 图片 / 扫描件走多模态 OCR 式抽取
            response = p.complete_vision(_EXTRACT_SYSTEM, user, parsed.images)
        elif parsed.text.strip():
            response = p.complete(_EXTRACT_SYSTEM, user)
        else:
            raise LLMError("文档解析结果为空，无可抽取内容", "parse")
        candidate = parse_json_response(response)
        if not isinstance(candidate, dict):
            raise LLMError("抽取响应不是 JSON 对象", "parse")
        raw = candidate
        LOGGER.info(
            "文档抽取完成 provider=%s format=%s 字段数=%d",
            info.name, parsed.format, len(raw.get("fields", {}) or {}),
        )
    except (LLMError, ValueError, TypeError) as exc:
        # 不再降级到关键词占位模板；抽取结果必须是真实模型输出
        LOGGER.error("文档抽取失败 provider=%s：%s", info.name, type(exc).__name__)
        raise LLMError(
            f"文档抽取失败（{info.name}）：{exc}；请配置 LLM_API_KEY 后重试",
            getattr(exc, "kind", "extract"),
        ) from exc

    if embedded_tables and not raw.get("tables"):
        raw["tables"] = embedded_tables
        warnings.append("DOCX 内嵌表格已直接结构化")

    result = _normalize_result(raw, parsed, schema, info, warnings)
    result["provider"] = meta["provider"]
    result["mode"] = meta["mode"]
    meta["elapsed"] = round(time.monotonic() - started, 3)
    meta["warnings"] = warnings
    meta["pages"] = parsed.meta.get("pages", 1)
    return result, meta


def extract_documents_batch(
    paths: Sequence[str],
    schema: Optional[List[Dict[str, Any]]] = None,
    provider: Optional[LLMProvider] = None,
    encoding: str = "utf-8",
    eco: bool = False,
    eco_config: Optional[Dict[str, Any]] = None,
    max_workers: Optional[int] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """批量抽取：多份文档并发处理，单份失败不影响整批。

    ``on_progress(done, total, name)`` 为可选回调，每完成一份调用一次
    （线程安全供异步任务页展示进度）；同步调用不传即可，行为不变。

    性能约定：
    - Provider 只创建一次并在整批复用（避免重复读 .env / 建连接）；
    - LLM 调用是 I/O 密集，用线程池并发，默认最多 4 路，上限 8 路，
      避免打爆 API 限流或本地内存；
    - 解析结果不落盘、不重复读取；汇总报表在导出层一次性生成；
    - 返回结构化汇总，含每份状态、节能模式 token 节省与失败原因。
    """
    started = time.monotonic()
    paths = [str(p) for p in (paths or []) if str(p).strip()]
    if not paths:
        raise ExtractionError("批量抽取至少需要一份文档")
    schema = schema or list(DEFAULT_SCHEMA)
    cfg = EcoConfig.from_dict(eco_config)
    workers = max(1, min(int(max_workers or cfg.max_workers), len(paths), 8))
    p = provider or get_provider()  # 整批复用，只创建一次

    def one(path: str) -> Dict[str, Any]:
        try:
            result, meta = extract_document(
                path, schema, provider=p, encoding=encoding,
                eco=eco, eco_config=cfg.to_dict(),
            )
            return {"path": path, "name": Path(path).name, "status": "ok",
                    "result": result, "meta": meta, "error": ""}
        except Exception as exc:  # noqa: BLE001 - 单份失败不中断整批
            LOGGER.warning("批量抽取单份失败 %s：%s", path, type(exc).__name__)
            return {"path": path, "name": Path(path).name, "status": "error",
                    "result": None, "meta": {}, "error": str(exc)[:300]}

    done = {"n": 0}
    done_lock = Lock()

    def one_with_progress(path: str) -> Dict[str, Any]:
        item = one(path)
        if on_progress is not None:
            with done_lock:
                done["n"] += 1
                current = done["n"]
            try:
                on_progress(current, len(paths), Path(path).name)
            except Exception as exc:  # noqa: BLE001 - 进度回调不能影响抽取结果
                LOGGER.warning("批量抽取进度回调失败：%s", type(exc).__name__)
        return item

    if on_progress is not None:
        try:
            on_progress(0, len(paths), "开始批量抽取")
        except Exception:  # noqa: BLE001
            pass
    if workers == 1:
        documents = [one_with_progress(x) for x in paths]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            documents = list(pool.map(one_with_progress, paths))

    ok = [d for d in documents if d["status"] == "ok"]
    eco_items = [d["meta"].get("eco") for d in ok if d["meta"].get("eco")]
    original = sum(int(x.get("original_chars") or 0) for x in eco_items)
    sent = sum(int(x.get("sent_chars") or 0) for x in eco_items)
    meta: Dict[str, Any] = {
        "total": len(paths), "succeeded": len(ok),
        "failed": len(paths) - len(ok), "workers": workers,
        "eco": eco, "elapsed": round(time.monotonic() - started, 3),
        "eco_original_chars": original, "eco_sent_chars": sent,
        "eco_saved_ratio": round(1 - sent / original, 4) if original else 0.0,
        "eco_fallbacks": sum(1 for x in eco_items if x.get("fallback")),
    }
    LOGGER.info(
        "批量抽取完成 总数=%d 成功=%d 失败=%d 并发=%d 节能字符 %d→%d",
        meta["total"], meta["succeeded"], meta["failed"], workers, original, sent,
    )
    return {"documents": documents, "results": [d["result"] for d in ok],
            "errors": [{"name": d["name"], "error": d["error"]}
                       for d in documents if d["status"] == "error"],
            "meta": meta, "schema": schema}


def parse_schema(text: str) -> List[Dict[str, Any]]:
    """从 Web 表单文本解析字段定义：每行一个字段名。

    支持 ``*名称:类型``（``*`` 表示必填）与可选的第三段定位关键词
    ``*名称:类型:别名1|别名2``，第三段供节能模式做语义定位。
    """
    schema: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        required = line.startswith("*")
        line = line.lstrip("*").strip()
        sep = ":" if ":" in line else ("：" if "：" in line else "")
        if sep:
            parts = [p.strip() for p in line.split(sep)]
        else:
            parts = [line, "string"]
        name = parts[0]
        typ = (parts[1] if len(parts) > 1 else "string").lower()
        if name and typ in ("string", "number", "date", "bool"):
            entry: Dict[str, Any] = {"name": name, "type": typ, "required": required}
            if len(parts) > 2 and parts[2]:
                entry["keywords"] = [
                    x.strip() for x in re.split(r"[|,，、;；]+", parts[2]) if x.strip()
                ]
            schema.append(entry)
    return schema or list(DEFAULT_SCHEMA)


def schema_to_text(schema: List[Dict[str, Any]]) -> str:
    """schema 回转可编辑文本，供 Web 表单回显（含可选定位关键词）。"""
    lines = []
    for s in schema:
        line = ("*" if s.get("required") else "") + \
            f"{s.get('name', '')}:{s.get('type', 'string')}"
        kws = s.get("keywords")
        if isinstance(kws, (list, tuple)) and kws:
            line += ":" + "|".join(str(x) for x in kws)
        elif isinstance(kws, str) and kws.strip():
            line += ":" + kws.strip()
        lines.append(line)
    return "\n".join(lines)

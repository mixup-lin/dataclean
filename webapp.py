#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Web 在线查看工作台（仅绑定 127.0.0.1）。

三页：
  /          自然语言工作台（输入需求 → 生成规则 → 校验执行）
  /result/*  结果查看（规则回看、指标、分页结果表、导出下载）
  /extract   文档结构化抽取（输入 → 抽取 → 结构化输出）

实现约定
--------
- 服务端渲染 + 纯 HTML 表单交互，**零 JS 框架依赖**；页面模板为
  ``pages/*.html``，样式全部在 ``static/style.css``。执行等待状态用一小段
  渐进增强脚本（禁用 JS 时表单仍可正常提交），后台任务的进度页则用
  ``meta refresh`` 自动轮询，无需任何前端框架。
- Flask 可用时用 Flask；缺失时自动降级到标准库 ``http.server``，
  两者共用 ``handle_request``，功能等价，保证离线可演示。
- 所有数值展示复用 ``_fmt_num``（禁用科学计数法）；下载路径经
  ``exporters.resolve_artifact`` 双重白名单校验，杜绝目录穿越。
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode

from llm_provider import describe_provider, get_provider
from jobs import get_manager as get_job_manager
from charts_svg import render_chart
from exporters import (
    list_runs,
    load_run,
    new_run_dir,
    resolve_artifact,
    export_extraction,
    read_page,
)
from etl_pipeline import _esc, _fmt_num, _svg_gauge
from sources import save_upload

LOGGER = logging.getLogger("etl")

try:
    from flask import Flask, Response as FlaskResponse, request
except ImportError:  # pragma: no cover
    Flask = None  # type: ignore
    FlaskResponse = None  # type: ignore
    request = None  # type: ignore

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"
STATIC = ROOT / "static"
SAMPLES = ROOT / "samples"

# HTTP 层请求体上限：应用层 save_upload 有 200MB / 文档 30MB 的限制，但那一步
# 在整包读入内存之后才执行。这里在传输层先拦截超大请求，避免内存耗尽（DoS）。
# 需大于单个上传上限，留出 multipart 边界与多文件开销。
MAX_BODY_BYTES = 256 * 1024 * 1024  # 256 MiB

# 执行等待层：纯 CSS + 一小段渐进增强脚本（禁用 JS 时表单仍可提交）。
# 提交后立即显示阶段文案 / 计时 / 进度条，并禁用提交按钮防止重复请求；
# 浏览器导航或 bfcache 返回时自动隐藏。
WAIT_HTML = """
<div id="dc-wait" class="dc-wait" aria-hidden="true">
  <div class="dc-wait-card" role="status" aria-live="polite">
    <div class="dc-spinner"><i></i><i></i><i></i></div>
    <div class="dc-wait-title" id="dc-wait-title">正在处理…</div>
    <div class="dc-wait-stage" id="dc-wait-stage">读取数据源…</div>
    <div class="dc-wait-track"><div class="dc-wait-bar" id="dc-wait-bar"></div></div>
    <div class="dc-wait-foot">已等待 <b id="dc-wait-sec">0</b> 秒 · 处理期间请勿重复提交</div>
  </div>
</div>
<script>
(function () {
  var overlay = document.getElementById('dc-wait');
  if (!overlay) { return; }
  var title = document.getElementById('dc-wait-title');
  var stage = document.getElementById('dc-wait-stage');
  var bar = document.getElementById('dc-wait-bar');
  var sec = document.getElementById('dc-wait-sec');
  var stages = [], start = 0, timer = null;
  function hide() {
    overlay.classList.remove('on');
    overlay.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('dc-wait-lock');
    if (timer) { clearInterval(timer); timer = null; }
    var disabled = document.querySelectorAll('[data-dc-disabled]');
    for (var i = 0; i < disabled.length; i++) {
      disabled[i].disabled = false;
      disabled[i].removeAttribute('data-dc-disabled');
    }
  }
  function tick() {
    var s = Math.floor((Date.now() - start) / 1000);
    sec.textContent = String(s);
    if (stages.length) {
      var idx = Math.min(stages.length - 1, Math.floor(s / 3));
      stage.textContent = stages[idx];
      bar.style.width = Math.min(96, 8 + idx * 22 + (s % 3) * 3) + '%';
    }
  }
  function show(form, btn) {
    var label = (btn && (btn.getAttribute('data-wait') || btn.textContent)) || '正在处理…';
    title.textContent = String(label).trim();
    var raw = form.getAttribute('data-stages') ||
      '读取数据源,执行清洗与类型转换,运行算子步骤,生成结果与报告';
    stages = raw.split(',').filter(Boolean);
    stage.textContent = stages[0];
    bar.style.width = '8%';
    sec.textContent = '0';
    start = Date.now();
    overlay.classList.add('on');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.classList.add('dc-wait-lock');
    if (timer) { clearInterval(timer); }
    timer = setInterval(tick, 500);
    var buttons = form.querySelectorAll('button[type=submit], input[type=submit]');
    setTimeout(function () {
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].setAttribute('data-dc-disabled', '1');
        buttons[i].disabled = true;
      }
    }, 0);
  }
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || String(form.method).toLowerCase() !== 'post') { return; }
    var btn = e.submitter || null;
    if (btn && btn.getAttribute('data-no-wait') === '1') { return; }
    show(form, btn);
  }, false);
  window.addEventListener('pageshow', hide);
  window.addEventListener('beforeunload', hide);
})();
</script>
"""

JOBS = get_job_manager()

EXAMPLE_INTENTS = [
    "中位数填充缺失值，剔除常数列，导出 csv",
    "统计成交房源的成交占比（成交价不为零）",
    "各小区成交套数占比，按占比降序，帕累托",
    "成交价最高的前 10 个小区",
    "按月统计成交套数的趋势",
    "把成交价分成 5 组看分布",
    "按区县和物业类型做交叉表透视",
    "筛选起拍价大于 0 并按成交价降序排序",
]

TASK_CHIPS = [
    ("占比", "各小区成交套数占比，按占比降序，帕累托"),
    ("排名", "成交价最高的前 10 个小区"),
    ("趋势", "按月统计成交套数的趋势"),
    ("分布", "把成交价分成 5 组看分布"),
    ("透视", "按区县和物业类型做交叉表透视"),
    ("多表合并", "按小区关联维表后统计成交套数并排名"),
]

CONTRACT_RULES = """
<div class="contract-grid">
  <div class="contract-box"><div class="t">① 输入 INPUT</div><div class="d">自然语言需求描述（intent）、主表与最多 2 个附加表（sources）、可选列名清单（columns）、可选列名映射（column_aliases：中文描述 = 真实列名）；文件可上传或填写路径。描述词与列名的对应关系只认使用者给的映射，程序不内置语义词典。</div></div>
  <div class="contract-box"><div class="t">② 规则生成 RULE</div><div class="d">LLM 输出规则 JSON：version / intent / task_type / source(s) / column_aliases / options / steps / output；也可直接提交显式规则 JSON（无需 LLM）。经字段类型、枚举、算子注册表与每个数据源的路径安全校验。</div></div>
  <div class="contract-box"><div class="t">③ 规则执行 EXECUTE</div><div class="d">options 映射为 PipelineConfig 复用清洗与聚合；steps 顺序执行 50+ 个声明式算子（filter/date_filter/count_rows/rate/sort/aggregate/pct/pivot/join/rank/bin/date_part/resample/rolling/convert_unit/assert/mask_pii…），每个算子的角色由注册表声明；判空类条件列在清洗后自动还原原始缺失，避免填充抹掉「为空/不为空」语义。编译层会把「判空打标 + 分组计数」确定性归一为 rate，直接产出两类条数与比率。join 前对齐键类型并做膨胀保护。规则可保存到规则库，后续直接选择复用。</div></div>
  <div class="contract-box"><div class="t">④ 输出 OUTPUT</div><div class="d">任务结果 CSV / Excel（多 sheet）/ JSON、附表 table_N.csv、rule.json、execution_summary.json、质量报告与任务报告 HTML。</div></div>
</div>
"""

CONTRACT_EXTRACT = """
<div class="contract-grid">
  <div class="contract-box"><div class="t">① 输入 INPUT</div><div class="d">上传（可多选批量）/ 服务器已有文件下拉 / 手填路径，三选一或叠加；字段定义 Schema 支持「*名称:类型:定位别名1|别名2」。<b>处理方式可选同步（等待完成直接出结果）或异步（提交后立即返回任务号，后台线程执行并展示实时进度，可从「后台任务」列表随时取回）</b>。性能：整批复用同一 LLM Provider，线程池并发默认 4 路、上限 8 路，单份失败不中断整批；每完成一份即上报进度。</div></div>
  <div class="contract-box"><div class="t">② 解析 PARSE</div><div class="d">统一中间结构 ParsedDocument：text（分页文本）、pages、images（mime + bytes）、meta；可选依赖缺失时给出安装提示。</div></div>
  <div class="contract-box"><div class="t">③ 抽取 EXTRACT</div><div class="d">文本走 LLM 结构化抽取；图片走 complete_vision 多模态 OCR。<b>节能模式</b>：先按字段语义本地定位关键词（不给 LLM），取命中位置前后 N 字符的滑动窗口、合并重叠与限量后仅把片段交给 LLM；完全定位不到则自动回退全文。</div></div>
  <div class="contract-box"><div class="t">④ 输出 OUTPUT</div><div class="d">fields / tables / confidence / evidence 全量 JSON；CSV 与 Excel 采用<b>横向宽表</b>——第一行即字段名，每行一份文档。批量额外产出 extraction_batch.json / .csv / .xlsx 汇总表，可直接送入清洗管道。</div></div>
</div>
"""


# ============================================================
# 响应对象与模板渲染
# ============================================================
@dataclass
class Reply:
    status: int = 200
    body: Any = ""
    content_type: str = "text/html; charset=utf-8"
    headers: Dict[str, str] = field(default_factory=dict)


def render(page: str, **values: Any) -> str:
    """读取 pages/*.html 并替换 {{KEY}} 占位符（不依赖模板引擎）。"""
    text = (PAGES / page).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    # 所有页面统一注入等待层；模板里也可用 {{LOADING}} 显式指定位置
    if "{{LOADING}}" in text:
        text = text.replace("{{LOADING}}", WAIT_HTML)
    elif "id=\"dc-wait\"" not in text:
        text = text.replace("</body>", WAIT_HTML + "\n</body>", 1)
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "", text)
    return text


def _view_params(query: Dict[str, List[str]]) -> Dict[str, str]:
    def one(key: str, default: str) -> str:
        vals = query.get(key)
        return vals[0] if vals else default

    return {
        "grid": one("grid", "0"),
        "notes": one("notes", "1"),
        "zoom": one("zoom", "1"),
    }


def _body_class(v: Dict[str, str]) -> str:
    cls = []
    if v["grid"] == "1":
        cls.append("grid-on")
    if v["notes"] == "0":
        cls.append("notes-off")
    return " ".join(cls)


def _nav(active: str) -> str:
    try:
        p = describe_provider(get_provider())
    except Exception as exc:  # noqa: BLE001 - 未配置 LLM 时页面仍应可用（可用显式规则）
        p = None
        config_error = str(exc)
    else:
        config_error = ""
    configured = p is not None
    badge_cls = "badge" if configured else "badge offline"
    badge_text = "在线 LLM" if configured else "LLM 未配置"
    badge_title = (
        f"在线模型：{p.model}" if configured
        else (config_error + "；可使用显式规则 JSON 离线执行")
    )
    model_label = p.model if configured else "需配置 LLM_API_KEY"
    links = [
        ("workbench", "/", "自然语言工作台"),
        ("result", "/workbench-runs", "结果查看"),
        ("extract", "/extract", "文档抽取"),
        ("jobs", "/jobs", "后台任务"),
    ]
    items = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    )
    return (
        '<header class="topbar"><div class="brand">'
        '<div class="brand-mark">DC</div><div>DataClean<span class="brand-sub">/ ETL Console</span></div>'
        '</div><nav class="nav">' + items + '</nav><div class="topbar-right">'
        f'<span class="{badge_cls}" title="{_esc(badge_title)}">'
        f'<span class="dot"></span>{_esc(badge_text)} · {_esc(model_label)}</span>'
        '</div></header>'
    )


def _footer() -> str:
    return (
        '<footer class="footer">DataClean ETL Console · 服务端渲染 / 零 JS 框架依赖 / '
        '执行等待状态与后台任务进度 / 数值不使用科学计数法 · 由 etl_pipeline.py 与扩展模块驱动</footer>'
    )


def _controls(path: str, v: Dict[str, str]) -> str:
    try:
        zoom = max(0.8, min(1.3, float(v["zoom"])))
    except (TypeError, ValueError):
        zoom = 1.0

    def link(label: str, **changes: str) -> str:
        params = {**v, **changes}
        return f'<a href="{path}?{urlencode(params)}">{label}</a>'

    return (
        '<div class="view-controls"><div class="vc-title">VIEW</div>'
        + link("网格 " + ("开" if v["grid"] == "1" else "关"),
               grid="0" if v["grid"] == "1" else "1")
        + link("契约 " + ("显示" if v["notes"] == "1" else "隐藏"),
               notes="0" if v["notes"] == "1" else "1")
        + link("A+", zoom=str(round(min(1.3, zoom + 0.1), 2)))
        + link("A−", zoom=str(round(max(0.8, zoom - 0.1), 2)))
        + '</div>'
        + f'<style>body {{ --zoom: {_esc(str(zoom))}; }}</style>'
    )


_JSON_TOKEN_RE = re.compile(
    r'(?P<str>"(?:\\.|[^"\\])*")|(?P<num>-?\d+(?:\.\d+)?)|'
    r'(?P<bool>\b(?:true|false|null)\b)'
)


def _json_highlight(obj: Any) -> str:
    """JSON 语法着色：逐 token 转义，避免字符串内数字被重复着色或注入。"""
    raw = json.dumps(obj, ensure_ascii=False, indent=2)
    out: List[str] = []
    pos = 0
    for m in _JSON_TOKEN_RE.finditer(raw):
        out.append(_html.escape(raw[pos : m.start()]))
        token, kind = m.group(), m.lastgroup
        if kind == "str":
            is_key = raw[m.end() :].lstrip().startswith(":")
            cls = "k" if is_key else "s"
        else:
            cls = "n" if kind == "num" else "b"
        out.append(f'<span class="{cls}">{_html.escape(token)}</span>')
        pos = m.end()
    out.append(_html.escape(raw[pos:]))
    return "".join(out)


# ============================================================
# 页面片段
# ============================================================
def _chips(intent: str) -> str:
    return "".join(
        f'<a class="chip" href="/?{urlencode({"intent": e})}">{_esc(e)}</a>'
        for e in EXAMPLE_INTENTS
    )


def _format_options(current: str, name: str = "source_format") -> str:
    opts = ["csv", "json", "excel", "sqlite"]
    return f'<select name="{name}">' + "".join(
        f'<option value="{o}" {"selected" if o == current else ""}>{o}</option>'
        for o in opts
    ) + "</select>"


def _format_checks(selected: List[str]) -> str:
    return "".join(
        f'<label><input type="checkbox" name="formats" value="{f}" '
        f'{"checked" if f in selected or (not selected and f == "csv") else ""}> 导出 {f.upper()}</label>'
        for f in ("csv", "xlsx", "json")
    ) + '<label><input type="checkbox" name="report" value="1" checked> 生成质量报告</label>'


ALIAS_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fa5]{1,12}$")


def _task_chips(intent: str) -> str:
    return "".join(
        f'<a class="chip" href="/?{urlencode({"intent": e})}">{_esc(label)}</a>'
        for label, e in TASK_CHIPS
    )


def _pick_upload(files: Optional[Dict[str, List[Tuple[str, bytes]]]], key: str) -> Any:
    items = (files or {}).get(key) or []
    for filename, data in items:
        if data:
            return save_upload(filename or f"{key}.csv", data)
    return None


def _collect_sources(
    form: Dict[str, List[str]], files: Optional[Dict[str, List[Tuple[str, bytes]]]]
) -> Dict[str, Any]:
    """从表单 / 上传构建 sources（main + 最多 2 个附加表），返回完整 sources。"""
    from rules import RuleValidationError

    errors: List[str] = []
    main_upload = None
    try:
        main_upload = _pick_upload(files, "source_file")
    except Exception as exc:  # noqa: BLE001 - 上传失败逐条提示
        errors.append(f"主表上传失败：{exc}")
    main_path = _form_value(form, "source_path", "lots.csv").strip()
    if main_upload:
        main: Dict[str, Any] = {
            "path": main_upload["path"], "format": main_upload["format"],
        }
    else:
        main = {"path": main_path, "format": _form_value(form, "source_format", "csv")}
    if not main.get("path"):
        errors.append("主数据源路径不能为空")

    sources: Dict[str, Any] = {"main": main}
    for i in (1, 2):
        alias = _form_value(form, f"extra{i}_alias").strip()
        upload = None
        try:
            upload = _pick_upload(files, f"extra{i}_file")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"附加表 {i} 上传失败：{exc}")
        path = _form_value(form, f"extra{i}_path").strip()
        if not alias and not upload and not path:
            continue
        if not alias:
            errors.append(f"附加表 {i} 需要填写别名（引用维表时使用）")
            continue
        if alias == "main" or not ALIAS_RE.match(alias):
            errors.append(
                f"附加表别名 {alias!r} 非法：需 1-12 位中文 / 字母 / 数字 / 下划线，且不能是 main"
            )
            continue
        if upload:
            src: Dict[str, Any] = {"path": upload["path"], "format": upload["format"]}
        elif path:
            src = {"path": path, "format": _form_value(form, f"extra{i}_format", "csv")}
        else:
            errors.append(f"附加表 {alias} 需要上传文件或填写路径")
            continue
        key = _form_value(form, f"extra{i}_key").strip()
        right_key = _form_value(form, f"extra{i}_right_key").strip()
        how = _form_value(form, f"extra{i}_how", "left")
        if key:
            src["left_on"] = key
        if right_key or key:
            src["right_on"] = right_key or key
        if how in ("left", "inner", "right", "outer"):
            src["how"] = how
        sources[alias] = src

    if errors:
        raise RuleValidationError(errors)
    return sources


def _kpis_html(kpis: List[Dict[str, Any]]) -> str:
    cards = []
    for item in (kpis or [])[:6]:
        unit = str(item.get("unit") or "")
        value = item.get("value")
        text = _esc(value) if isinstance(value, str) else _esc(_fmt_num(value))
        if unit and text not in ("", "—"):
            text += f'<span class="unit">{_esc(unit)}</span>'
        cards.append(
            f'<div class="kpi"><div class="kpi-v">{text}</div>'
            f'<div class="kpi-l">{_esc(item.get("label", ""))}</div>'
            f'<div class="kpi-h">{_esc(item.get("hint", ""))}</div></div>'
        )
    return "".join(cards)


def _charts_html(charts: List[Dict[str, Any]], kinds: Any = ()) -> str:
    """渲染图表卡：``kinds`` 非空时只保留指定图表类型（面板按类型筛选）。"""
    allowed = {str(k) for k in (kinds or ())}
    picked = [
        c for c in (charts or [])
        if isinstance(c, dict) and (not allowed or str(c.get("kind")) in allowed)
    ][:3]
    blocks = []
    for chart in picked:
        try:
            svg = render_chart(chart)
        except Exception as exc:  # noqa: BLE001 - 单图失败不影响页面
            LOGGER.warning("图表渲染失败：%s", type(exc).__name__)
            continue
        if not svg:
            continue
        blocks.append(
            f'<div class="chart-card"><h3>{_esc(chart.get("title", "图表"))}</h3>'
            f'<div class="chart-body">{svg}</div></div>'
        )
    return "".join(blocks)


def _tabs_html(run_id: str, tables: List[Dict[str, Any]], current: int) -> str:
    if not tables:
        return ""
    items = []
    for idx, entry in enumerate(tables):
        cls = "tab active" if idx == current else "tab"
        items.append(
            f'<a class="{cls}" href="/result/{_esc(run_id)}?{urlencode({"table": idx})}">'
            f'{_esc(entry.get("name", f"表 {idx + 1}"))}</a>'
        )
    return '<div class="tabs">' + "".join(items) + "</div>"


def _task_table(
    summary: Dict[str, Any], base_output: str, run_id: str,
    table_idx: int, page_no: int,
) -> Tuple[Dict[str, Any], str]:
    tables = (summary.get("task") or {}).get("tables") or []
    if not tables:
        return read_page(summary.get("result_csv", ""), page_no, 20), "结果表"
    idx = max(0, min(table_idx, len(tables) - 1))
    entry = tables[idx]
    file = str(entry.get("file") or "result.csv")
    path = Path(base_output) / run_id / file
    return read_page(str(path), page_no, 20), str(entry.get("name", "结果表"))


def _runs_html(base_output: str) -> str:
    runs = list_runs(base_output)
    if not runs:
        return '<div class="alert info">尚无执行记录。提交一次需求后，这里会列出全部运行产物。</div>'
    rows = "".join(
        f'<tr><td><a href="/result/{_esc(r["run_id"])}">{_esc(r["run_id"])}</a></td>'
        f'<td>{_esc(str(r.get("created_at", "")))}</td>'
        f'<td><span class="inline-badge">{_esc((r.get("task") or {}).get("label", "—"))}</span></td>'
        f'<td>{_esc(_trunc_text(str(r.get("intent", "")), 40))}</td>'
        f'<td>{_esc(_fmt_num(r.get("rows", 0)))}</td>'
        f'<td>{_esc(_fmt_num(r.get("columns", 0)))}</td></tr>'
        for r in runs[:12]
    )
    return (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        '<th>运行 ID</th><th>时间</th><th>任务类型</th><th>需求描述</th><th>行数</th><th>列数</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table></div>'
    )


def _trunc_text(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _extra_values(sources: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """把已提交的附加表配置还原成表单值（用于校验失败后回填）。"""
    extras: List[Dict[str, str]] = []
    for alias, item in (sources or {}).items():
        if alias == "main" or not isinstance(item, dict):
            continue
        extras.append({
            "alias": str(alias),
            "path": str(item.get("path") or ""),
            "format": str(item.get("format") or "csv"),
            "key": str(item.get("left_on") or ""),
            "right_key": str(item.get("right_on") or ""),
            "how": str(item.get("how") or "left"),
        })
    while len(extras) < 2:
        extras.append({"alias": "", "path": "", "format": "csv", "key": "",
                       "right_key": "", "how": "left"})
    return extras[:2]


def _how_options(current: str) -> str:
    return "".join(
        f'<option value="{value}" {"selected" if value == current else ""}>{value}</option>'
        for value in ("left", "inner", "right", "outer")
    )


def _source_summary(sources: Optional[Dict[str, Any]]) -> str:
    if not sources:
        return "单表"
    extra = [a for a in sources if a != "main"]
    return "主表" + (f" + {len(extra)} 个附加表（{', '.join(extra)}）" if extra else "")


def _rule_task_badge(rule: Optional[Dict[str, Any]]) -> str:
    if not rule:
        return '<div class="alert info">尚未生成规则，生成后会显示任务类型徽章。</div>'
    try:
        from task_profile import TASK_LABELS, detect_task_type

        task_type = detect_task_type(rule)
        label = TASK_LABELS.get(task_type, task_type)
        ops = "·".join(str(s.get("op")) for s in rule.get("steps") or [] if isinstance(s, dict))
        return (
            f'<div class="alert info"><span class="task-badge" style="margin:0">'
            f'任务类型 · {_esc(label)}</span><br>'
            f'执行计划：{_esc(ops or "无 steps（仅清洗）")}<br>'
            f'数据源：{_esc(_source_summary(rule.get("sources")))}</div>'
        )
    except Exception:  # noqa: BLE001 - 徽章失败不影响页面
        return ""


def _column_hints_html(columns: List[str]) -> str:
    """把探测到的真实列名摆出来，方便使用者写「中文描述 = 真实列名」映射。"""
    if not columns:
        return (
            '<div class="alert info">未探测到列名：请确认路径与格式正确，'
            "或直接在「可用列名」里手工填写。</div>"
        )
    shown = columns[:80]
    chips = "".join(f'<span class="chip">{_esc(c)}</span>' for c in shown)
    more = f"，仅展示前 {len(shown)} 个" if len(columns) > len(shown) else ""
    return (
        '<div class="chips">' + chips + "</div>"
        f'<p class="hint">已探测 {len(columns)} 列{more}。需求里写中文描述，'
        "这里给出真实列名；两边不一致时用「描述 = 列名」逐行建立映射。</p>"
    )


def _rules_library_html(active_id: str = "") -> Tuple[str, str]:
    """渲染规则库的「下拉选项」与「管理卡片」两段 HTML。

    规则库文件损坏时 list_rules() 会自动跳过，页面不因此报错。
    """
    from rule_store import list_rules

    items = list_rules()
    options = ['<option value="">（不使用，按需求生成/使用规则输入框）</option>']
    rows: List[str] = []
    for item in items:
        selected = " selected" if item.id == active_id else ""
        label = f"{_esc(item.name)} · {item.op_count} 算子 · {_esc(item.updated_at)}"
        options.append(f'<option value="{_esc(item.id)}"{selected}>{label}</option>')
        rows.append(
            f'<tr><td><b>{_esc(item.name)}</b><br><span class="hint">{_esc(item.id)}</span></td>'
            f'<td>{_esc(item.intent[:70]) or "—"}</td>'
            f'<td>{item.op_count}</td><td>{_esc(item.updated_at)}</td>'
            f'<td class="row-actions">'
            f'<a class="btn btn-mini" href="/rules/load?id={_esc(item.id)}">载入编辑</a>'
            f'<form method="post" action="/rules/execute" class="inline-form">'
            f'<input type="hidden" name="rule_id" value="{_esc(item.id)}">'
            f'<button class="btn btn-mini btn-primary" type="submit" '
            f'data-wait="正在执行已保存的规则…">直接执行</button></form>'
            f'<form method="post" action="/rules/delete" class="inline-form">'
            f'<input type="hidden" name="rule_id" value="{_esc(item.id)}">'
            f'<button class="btn btn-mini btn-ghost" type="submit" data-no-wait="1">删除</button>'
            f'</form></td></tr>'
        )
    if rows:
        body = (
            '<div class="table-scroll"><table class="data-table"><thead><tr>'
            '<th>规则名称</th><th>原始需求</th><th>算子数</th><th>更新时间</th><th>操作</th>'
            '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
        )
    else:
        body = '<div class="alert info">规则库为空。生成规则后在右侧填写名称并点击「保存到规则库」。</div>'
    return "".join(options), body


def _workbench(
    query: Dict[str, List[str]], base_output: str,
    intent: str = "", source_path: str = "lots.csv", source_format: str = "csv",
    columns: str = "", rule: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None, errors: Optional[List[str]] = None,
    formats: Optional[List[str]] = None,
    sources: Optional[Dict[str, Any]] = None,
    column_aliases: str = "",
    rule_input_json: str = "",
    rule_name: str = "",
    saved_rule_id: str = "",
    schema_mode: bool = False,
) -> Reply:
    v = _view_params(query)
    err_html = ""
    if errors:
        err_html = '<div class="alert error"><b>校验未通过：</b><br>' + "<br>".join(
            _esc(e) for e in errors
        ) + "</div>"
    meta_html = ""
    if meta:
        if meta.get("provider") == "rule-store":
            mode = "规则库"
        elif meta.get("mode") == "offline":
            mode = "显式规则"
        else:
            mode = "在线 LLM"
        alias_warn = [w for w in (meta.get("alias_warnings") or []) if w]
        applied = meta.get("column_aliases") or {}
        meta_html = (
            f'<div class="alert {"warn" if meta.get("degraded") else "info"}">'
            f'生成来源：{_esc(meta.get("provider", ""))} / {mode} ｜ '
            f'模型：{_esc(meta.get("model", ""))} ｜ 耗时：{_esc(_fmt_num(meta.get("elapsed", 0)))} 秒'
            + ("<br>本次由显式规则驱动，未经 LLM 生成。" if meta.get("mode") == "offline" else "")
            + (
                "<br>已应用列名映射：" + "、".join(
                    f"{_esc(k)} → {_esc(v)}" for k, v in applied.items()
                )
                if applied else ""
            )
            + (
                "<br>列名映射提示：" + "；".join(_esc(w) for w in alias_warn)
                if alias_warn else ""
            )
            + (
                f"<br>状态说明：{_esc(meta['reason'])}"
                if meta.get("reason") else ""
            )
            + "</div>"
        )
    rule_raw = json.dumps(rule, ensure_ascii=False, indent=2) if rule else ""
    rule_preview = (
        f'<details class="fold" open><summary>规则语法高亮预览</summary>'
        f'<div><pre class="code-block">{_json_highlight(rule)}</pre></div></details>'
        if rule else '<div class="alert info">尚未生成规则。填写需求后点击「生成规则 JSON」。</div>'
    )
    main_src = (sources or {}).get("main") if isinstance(sources, dict) else None
    if isinstance(main_src, dict) and main_src.get("path"):
        source_path = str(main_src["path"])
        source_format = str(main_src.get("format") or source_format)
    extras = _extra_values(sources)
    hinted: List[str] = []
    if source_path.strip():
        try:
            from rules import probe_columns

            hinted = probe_columns({
                "path": source_path.strip(), "format": source_format or "csv",
            })
        except Exception:  # noqa: BLE001 - 探测失败不影响页面渲染
            hinted = []
    saved_options, library_html = _rules_library_html(saved_rule_id)
    body = render(
        "workbench.html",
        BODY_CLASS=_body_class(v), NAV=_nav("workbench"),
        INTENT=_esc(intent), SOURCE_PATH=_esc(source_path),
        SOURCE_FORMAT_OPTIONS=_format_options(source_format),
        COLUMNS=_esc(columns), EXAMPLES=_task_chips(intent),
        COLUMN_ALIASES=_esc(column_aliases),
        RULE_NAME=_esc(rule_name), SAVED_RULE_OPTIONS=saved_options,
        RULES_LIBRARY=library_html,
        RULE_INPUT_JSON=_esc(rule_input_json),
        COLUMN_HINTS=_column_hints_html(hinted),
        FORMAT_CHECKS=_format_checks(formats or []),
        SCHEMA_MODE_CHECKED="checked" if schema_mode else "",
        ERRORS=err_html, RULE_META=meta_html, RULE_TASK=_rule_task_badge(rule),
        RULE_JSON_RAW=_esc(rule_raw), RULE_PREVIEW=rule_preview,
        CONTRACT=CONTRACT_RULES, RUNS=_runs_html(base_output),
        EXTRA1_OPEN="open" if extras[0]["alias"] else "",
        EXTRA1_ALIAS=_esc(extras[0]["alias"]), EXTRA1_PATH=_esc(extras[0]["path"]),
        EXTRA1_FORMAT_OPTIONS=_format_options(extras[0]["format"], "extra1_format"),
        EXTRA1_KEY=_esc(extras[0]["key"]),
        EXTRA1_RIGHT_KEY=_esc(extras[0]["right_key"]),
        EXTRA1_HOW_OPTIONS=_how_options(extras[0]["how"]),
        EXTRA2_OPEN="open" if extras[1]["alias"] else "",
        EXTRA2_ALIAS=_esc(extras[1]["alias"]), EXTRA2_PATH=_esc(extras[1]["path"]),
        EXTRA2_FORMAT_OPTIONS=_format_options(extras[1]["format"], "extra2_format"),
        EXTRA2_KEY=_esc(extras[1]["key"]),
        EXTRA2_RIGHT_KEY=_esc(extras[1]["right_key"]),
        EXTRA2_HOW_OPTIONS=_how_options(extras[1]["how"]),
        STEP2="active" if rule else "", STEP3="", STEP4="",
        FOOTER=_footer(), CONTROLS=_controls("/", v),
    )
    return Reply(body=body)


def _metrics_html(metrics: Dict[str, Any]) -> str:
    return "".join(
        f'<div class="metric"><div class="v">{_esc(_fmt_num(val))}</div>'
        f'<div class="l">{_esc(key)}</div></div>'
        for key, val in metrics.items()
    )


def _stages_html(stages: List[Dict[str, Any]]) -> str:
    max_t = max([float(s.get("elapsed", 0)) for s in stages] + [0.001])
    items = "".join(
        f'<div class="stage-item"><span class="nm">{_esc(s["name"])}</span>'
        f'<div class="bar" style="width:{max(8, 160 * float(s.get("elapsed", 0)) / max_t):.0f}px"></div>'
        f'<span class="tm">{_esc(_fmt_num(s.get("elapsed", 0)))} s</span></div>'
        for s in stages
    )
    return f'<div class="stage-list">{items}</div>'


def _step_logs_html(logs: List[Dict[str, Any]]) -> str:
    if not logs:
        return '<div class="alert info">本次规则未包含整理/统计步骤。</div>'
    rows = "".join(
        f'<tr><td>{_esc(_fmt_num(l["index"]))}</td><td>{_esc(l["op"])}</td>'
        f'<td>{_esc(l["status"])}</td>'
        f'<td>{_esc(_fmt_num(l["rows_before"]))} → {_esc(_fmt_num(l["rows_after"]))}</td>'
        f'<td>{_esc(l["note"])}</td></tr>'
        for l in logs
    )
    return (
        '<div class="table-scroll" style="margin-top:14px"><table class="data-table">'
        '<thead><tr><th>#</th><th>算子</th><th>状态</th><th>行数变化</th><th>说明</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _table_html(page_data: Dict[str, Any]) -> str:
    cols = page_data["columns"]
    if not cols:
        return '<div class="alert warn">结果 CSV 不可读或为空。</div>'
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    rows = "".join(
        "<tr>" + "".join(f"<td>{_esc(r.get(c, ''))}</td>" for c in cols) + "</tr>"
        for r in page_data["rows"]
    )
    return (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        + head + "</tr></thead><tbody>" + rows + "</tbody></table></div>"
    )


def _pager_html(run_id: str, page_data: Dict[str, Any], v: Dict[str, str]) -> str:
    cur, pages = page_data["page"], page_data["pages"]
    items = []
    for p in range(max(1, cur - 2), min(pages, cur + 2) + 1):
        cls = "cur" if p == cur else ""
        items.append(
            f'<a class="{cls}" href="/result/{_esc(run_id)}?{urlencode({"page": p, **v})}">{p}</a>'
        )
    return (
        f'<div class="pager"><span>共 {_esc(_fmt_num(page_data["total"]))} 行 / '
        f'{_esc(_fmt_num(pages))} 页，当前第 {_esc(_fmt_num(cur))} 页</span>'
        + "".join(items) + "</div>"
    )


def _artifacts_html(run_id: str, artifacts: List[Dict[str, Any]]) -> str:
    icons = {"data": "📄", "rule": "🧩", "report": "📊", "summary": "📋", "extraction": "🗂"}
    return "".join(
        f'<a class="artifact" href="/download/{_esc(run_id)}/{quote(a["name"])}">'
        f'<span class="ico">{icons.get(a.get("kind", ""), "📄")}</span>'
        f'<span><span class="nm">{_esc(a.get("label", a["name"]))}</span><br>'
        f'<span class="sz">{_esc(a["name"])} · {_esc(_fmt_num(a.get("size", 0)))} B</span></span></a>'
        for a in artifacts
    )


# ============================================================
# 结果页面板流：按后端 panels 规格分派渲染
# ============================================================
_DEFAULT_PANELS: List[Dict[str, Any]] = [
    {
        "kind": "kpi", "title": "指标概览", "hint": "任务关键指标",
        "source": "kpis", "chart_kinds": [], "count": 0,
    },
    {
        "kind": "chart", "title": "图表概览", "hint": "聚合后的小数据可视化",
        "source": "charts", "chart_kinds": [], "count": 0,
    },
    {
        "kind": "table", "title": "结果明细", "hint": "分页读取结果表，可切换附表",
        "source": "tables", "chart_kinds": [], "count": 0,
    },
]


def _panel_shell(
    panel: Dict[str, Any], body: str, badge: str = "", wide: bool = False
) -> str:
    """面板外壳：标题 / 提示 / 轻量徽标 + 主体；宽面板（表格、关联）独占整行。"""
    kind = _esc(panel.get("kind") or "note")
    title = _esc(panel.get("title") or "面板")
    hint = str(panel.get("hint") or "")
    hint_html = f'<p class="panel-hint">{_esc(hint)}</p>' if hint else ""
    badge_html = f'<span class="panel-count">{_esc(badge)}</span>' if badge else ""
    cls = f"panel panel-{kind}" + (" panel-wide" if wide else "")
    return (
        f'<section class="{cls}"><div class="panel-head">'
        f'<h3 class="panel-title">{title}</h3>{badge_html}</div>'
        f'{hint_html}<div class="panel-body">{body}</div></section>'
    )


def _join_panel_html(join_stats: List[Dict[str, Any]]) -> str:
    """关联质量面板：每个附加表一块，展示匹配行 / 匹配率 / 主表未匹配。"""
    items = []
    for info in join_stats[:4]:
        alias = _esc(info.get("alias") or "附加表")
        how = _esc(f'{info.get("how") or "left"} join')
        matched = _esc(_fmt_num(info.get("matched", 0)))
        rate = _esc(_fmt_num(info.get("match_rate", 0)))
        unmatched = _esc(_fmt_num(info.get("unmatched_left", 0)))
        items.append(
            '<div class="join-item">'
            f'<div class="join-head"><span class="join-alias">{alias}</span>'
            f'<span class="join-how">{how}</span></div>'
            '<div class="join-metrics">'
            f'<div><div class="jv">{matched}</div><div class="jl">匹配行数</div></div>'
            f'<div><div class="jv">{rate}<span class="unit">%</span></div>'
            '<div class="jl">匹配率</div></div>'
            f'<div><div class="jv">{unmatched}</div><div class="jl">主表未匹配</div></div>'
            "</div></div>"
        )
    return '<div class="join-grid">' + "".join(items) + "</div>"


def _panels_html(
    task: Dict[str, Any], summary: Dict[str, Any], base_output: str, run_id: str,
    table_idx: int = 0, page_no: int = 1,
    view_params: Optional[Dict[str, str]] = None,
    page_data: Optional[Dict[str, Any]] = None,
    table_name: str = "",
) -> str:
    """按 ``task["panels"]`` 声明渲染面板流；空载荷跳过，缺规格回退默认三件套。"""
    task = task if isinstance(task, dict) else {}
    kpis = [k for k in (task.get("kpis") or []) if isinstance(k, dict)][:6]
    charts = [c for c in (task.get("charts") or []) if isinstance(c, dict)]
    tables = [t for t in (task.get("tables") or []) if isinstance(t, dict)]
    joins = [j for j in (task.get("join") or []) if isinstance(j, dict)]
    note = str(task.get("note") or "")
    vv = {**(view_params or {}), "table": str(table_idx)}
    if page_data is None or not table_name:
        loaded, loaded_name = _task_table(
            summary, base_output, run_id, table_idx, page_no
        )
        page_data = page_data if page_data is not None else loaded
        table_name = table_name or loaded_name
    page_data = {
        "columns": [], "rows": [], "total": 0, "page": 1, "pages": 1,
        **(page_data or {}),
    }
    declared = [
        p for p in (task.get("panels") or []) if isinstance(p, dict) and p.get("kind")
    ]
    # 有独立说明面板时，表格面板不再重复附注
    has_note_panel = any(str(p.get("kind")) == "note" for p in declared)

    def render_panel(panel: Dict[str, Any]) -> str:
        kind = str(panel.get("kind") or "")
        if kind == "kpi":
            if not kpis:
                return ""
            body = f'<div class="kpi-band">{_kpis_html(kpis)}</div>'
            return _panel_shell(panel, body, f"{len(kpis)} 项指标")
        if kind == "chart":
            body = _charts_html(charts, panel.get("chart_kinds") or [])
            if not body:
                return ""
            return _panel_shell(panel, f'<div class="chart-grid">{body}</div>')
        if kind == "join":
            if not joins:
                return ""
            return _panel_shell(panel, _join_panel_html(joins), wide=True)
        if kind == "table":
            if not tables and not page_data.get("columns"):
                return ""
            tabs_html = _tabs_html(run_id, tables, table_idx)
            meta = (
                f'<p class="panel-meta">当前表 · {_esc(table_name)}</p>'
                if tabs_html else ""
            )
            note_html = (
                f'<p class="panel-note">{_esc(note)}</p>'
                if note and not has_note_panel else ""
            )
            body = (
                f"{tabs_html}{meta}{_table_html(page_data)}"
                f'{_pager_html(run_id, page_data, vv)}{note_html}'
            )
            return _panel_shell(
                panel, body, f'共 {_fmt_num(page_data.get("total", 0))} 行', wide=True,
            )
        if kind == "note":
            if not note:
                return ""
            return _panel_shell(panel, f'<p class="panel-note">{_esc(note)}</p>', wide=True)
        LOGGER.warning("未知面板类型：%s", kind)
        return ""

    blocks = [html for html in (render_panel(p) for p in declared) if html]
    if not blocks:  # 规格缺失 / 载荷为空：回退默认组合，避免白屏
        blocks = [html for html in (render_panel(p) for p in _DEFAULT_PANELS) if html]
    if not blocks:
        return ""
    return '<div class="panel-flow">' + "".join(blocks) + "</div>"


def _result_page(
    query: Dict[str, List[str]], run_id: str, base_output: str
) -> Reply:
    summary = load_run(run_id, base_output)
    if summary is None:
        return Reply(status=404, body="<h1>运行记录不存在</h1>")
    v = _view_params(query)
    try:
        page_no = int((query.get("page") or ["1"])[0])
    except ValueError:
        page_no = 1
    try:
        table_idx = int((query.get("table") or ["0"])[0])
    except ValueError:
        table_idx = 0

    task = summary.get("task") or {}
    tables = task.get("tables") or []
    table_idx = max(0, min(table_idx, max(len(tables) - 1, 0)))
    page_data, table_name = _task_table(
        summary, base_output, run_id, table_idx, page_no
    )
    rule = summary.get("rule", {})
    rule_json = json.dumps(rule, ensure_ascii=False, indent=2)
    metrics = summary.get("metrics", {})
    warnings = list(summary.get("reconcile_warnings") or []) + list(
        summary.get("safety_warnings") or []
    )
    warn_html = ""
    if warnings:
        warn_html = '<div class="alert warn"><b>规则已自动规整 / 安全回退：</b><br>' + "<br>".join(
            _esc(w) for w in warnings
        ) + "</div>"
    artifacts = summary.get("artifacts", [])
    report_name = summary.get("report_name", "")
    task_report_name = summary.get("task_report_name", "")
    report_link = (
        f'/download/{quote(run_id)}/{quote(report_name)}' if report_name else "#"
    )
    task_report_link = (
        f'/download/{quote(run_id)}/{quote(task_report_name)}'
        if task_report_name else ""
    )
    task_badge = (
        f'<span class="task-badge">任务类型 · {_esc(task.get("label", "任务"))}</span>'
    )
    has_join_panel = any(
        str(p.get("kind")) == "join"
        for p in (task.get("panels") or []) if isinstance(p, dict)
    )
    join_note = ""
    if task.get("join") and not has_join_panel:
        info = task["join"][0]
        join_note = (
            f'<div class="alert info">关联 {_esc(str(info.get("alias", "")))}（'
            f'{_esc(str(info.get("how", "")))}）：匹配 {_esc(_fmt_num(info.get("matched", 0)))} 行，'
            f'匹配率 {_esc(_fmt_num(info.get("match_rate", 0)))}%，'
            f'主表未匹配 {_esc(_fmt_num(info.get("unmatched_left", 0)))} 行。</div>'
        )
    panels_html = _panels_html(
        task, summary, base_output, run_id, table_idx, page_no,
        view_params=v, page_data=page_data, table_name=table_name,
    )
    body = render(
        "result.html",
        BODY_CLASS=_body_class(v), NAV=_nav("result"),
        RUN_ID=_esc(run_id), CREATED_AT=_esc(summary.get("created_at", "")),
        ELAPSED=_esc(_fmt_num(summary.get("elapsed", 0))),
        PROVIDER=_esc(summary.get("provider", {}).get("provider", "unknown")),
        INTENT_SHORT=_esc(_trunc_text(str(summary.get("intent", "")), 14)),
        TASK_TITLE=_esc(task.get("title", "执行结果")),
        TASK_BADGE=task_badge,
        TASK_CONCLUSION=_esc(task.get("conclusion", "")),
        PANELS=panels_html,
        JOIN_NOTE=join_note,
        RULE_SUMMARY=f'{len(rule.get("steps", []))} 个算子 / {len(rule.get("options", {}))} 项配置',
        STAGE_COUNT=len(summary.get("stages", [])),
        ARTIFACT_COUNT=len(artifacts),
        WARNINGS=warn_html, METRICS=_metrics_html(metrics),
        GAUGE=_svg_gauge(int(metrics.get("质量评分", 0))),
        STAGES=_stages_html(summary.get("stages", [])),
        STEP_LOGS=_step_logs_html(summary.get("step_logs", [])),
        REPORT_LINK=report_link,
        TASK_REPORT_LINK=task_report_link,
        RULE_JSON=_json_highlight(rule), RULE_BYTES=_fmt_num(len(rule_json.encode("utf-8"))),
        ARTIFACTS=_artifacts_html(run_id, artifacts),
        CONTRACT=CONTRACT_RULES,
        FOOTER=_footer(), CONTROLS=_controls(f"/result/{run_id}", v),
    )
    return Reply(body=body)


def _doc_samples() -> str:
    candidates = [
        ("样例合同 TXT", "samples/contract.txt"),
        ("样例表格 TXT", "samples/table.txt"),
        ("真实数据 CSV", "lots.csv"),
    ]
    return "".join(
        f'<a class="chip" href="/extract?{urlencode({"doc_path": p})}">{_esc(label)}</a>'
        for label, p in candidates
    )


def _fields_html(result: Dict[str, Any]) -> str:
    fields = result.get("fields") or {}
    conf = result.get("confidence") or {}
    if not fields:
        return '<div class="alert info">尚未抽取。填写文档路径后点击「执行结构化抽取」。</div>'
    rows = "".join(
        f'<tr><td>{_esc(k)}</td><td>{_esc(v)}</td>'
        f'<td>{_esc(_fmt_num(conf.get(k, 0)))}</td></tr>'
        for k, v in fields.items()
    )
    return (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        '<th>字段</th><th>抽取值</th><th>置信度</th></tr></thead><tbody>'
        + rows + "</tbody></table></div>"
    )


def _tables_html(result: Dict[str, Any]) -> str:
    tables = result.get("tables") or []
    if not tables:
        return '<div class="alert info">本次未识别到表格结构。</div>'
    blocks = []
    for t in tables:
        cols = t.get("columns") or []
        head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
        rows = "".join(
            "<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in r[: len(cols)]) + "</tr>"
            for r in (t.get("rows") or [])
        )
        blocks.append(
            f'<div style="margin-top:12px"><b>{_esc(t.get("name", "table"))}</b>'
            '<div class="table-scroll"><table class="data-table"><thead><tr>'
            + head + "</tr></thead><tbody>" + rows + "</tbody></table></div></div>"
        )
    return "".join(blocks)


def _evidence_html(result: Dict[str, Any]) -> str:
    ev = result.get("evidence") or []
    if not ev:
        return ""
    items = "".join(
        f'<tr><td>{_esc(e.get("field", ""))}</td><td>{_esc(e.get("snippet", ""))}</td>'
        f'<td>{_esc(_fmt_num(e.get("page", 1)))}</td></tr>'
        for e in ev[:12]
    )
    return (
        '<details class="fold"><summary>原文证据（' + str(len(ev)) + ' 条）</summary><div>'
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        '<th>字段</th><th>原文片段</th><th>页</th></tr></thead><tbody>'
        + items + "</tbody></table></div></div></details>"
    )


def _eco_summary_html(meta: Optional[Dict[str, Any]]) -> str:
    """节能模式的 token 缩减摘要。"""
    eco = (meta or {}).get("eco")
    if not isinstance(eco, dict):
        return ""
    cls = "warn" if eco.get("fallback") else "info"
    state = "未定位到关键词，已回退全文" if eco.get("fallback") else "语义定位命中，仅送片段"
    return (
        f'<div class="alert {cls}"><b>节能模式：{state}</b><br>'
        f'原文 {_esc(_fmt_num(eco.get("original_chars", 0)))} 字符 → '
        f'送入 {_esc(_fmt_num(eco.get("sent_chars", 0)))} 字符 ｜ '
        f'命中 {_esc(_fmt_num(eco.get("hits", 0)))} 处 / '
        f'{_esc(_fmt_num(eco.get("snippets", 0)))} 个片段 ｜ '
        f'节省 {_esc(_fmt_num(round(float(eco.get("saved_ratio", 0)) * 100, 1)))}%'
        + (f'<br>原因：{_esc(eco.get("reason", ""))}' if eco.get("reason") else "")
        + "</div>"
    )


def _batch_summary_html(batch: Optional[Dict[str, Any]]) -> str:
    """批量抽取的逐份状态与汇总宽表预览。"""
    if not batch:
        return ""
    meta = batch.get("meta") or {}
    rows = []
    for doc in batch.get("documents") or []:
        eco = (doc.get("meta") or {}).get("eco") or {}
        status = "成功" if doc.get("status") == "ok" else "失败"
        rows.append(
            f'<tr><td>{_esc(doc.get("name", ""))}</td><td>{status}</td>'
            f'<td>{_esc(_fmt_num(eco.get("original_chars", 0))) if eco else "—"}</td>'
            f'<td>{_esc(_fmt_num(eco.get("sent_chars", 0))) if eco else "—"}</td>'
            f'<td>{_esc(doc.get("error", "") or "—")}</td></tr>'
        )
    table = (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        '<th>文档</th><th>状态</th><th>原文字符</th><th>送入字符</th><th>错误</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )
    eco_line = ""
    if meta.get("eco") and meta.get("eco_original_chars"):
        eco_line = (
            f'<br>整批节能：{_esc(_fmt_num(meta["eco_original_chars"]))} → '
            f'{_esc(_fmt_num(meta["eco_sent_chars"]))} 字符，节省 '
            f'{_esc(_fmt_num(round(float(meta.get("eco_saved_ratio", 0)) * 100, 1)))}%'
            f'；回退全文 {_esc(_fmt_num(meta.get("eco_fallbacks", 0)))} 份'
        )
    return (
        f'<div class="alert {"warn" if meta.get("failed") else "info"}">'
        f'<b>批量处理：共 {_esc(_fmt_num(meta.get("total", 0)))} 份，'
        f'成功 {_esc(_fmt_num(meta.get("succeeded", 0)))}，'
        f'失败 {_esc(_fmt_num(meta.get("failed", 0)))}，'
        f'并发 {_esc(_fmt_num(meta.get("workers", 1)))} 路，'
        f'耗时 {_esc(_fmt_num(meta.get("elapsed", 0)))} 秒</b>{eco_line}</div>' + table
    )


def _extract_page(
    query: Dict[str, List[str]], base_output: str,
    doc_path: str = "", schema_text: str = "",
    result: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    run_id: str = "", artifacts: Optional[List[Dict[str, Any]]] = None,
    errors: Optional[List[str]] = None,
    batch: Optional[Dict[str, Any]] = None,
    eco: bool = False, eco_window: int = 200,
    eco_max_snippets: int = 24, eco_max_chars: int = 12000,
    workers: int = 4, export_formats: Optional[List[str]] = None,
    mode: str = "sync", notice: str = "", job_id: str = "",
) -> Reply:
    from doc_extract import (
        DEFAULT_SCHEMA, ECO_DEFAULTS, list_local_documents, schema_to_text,
    )

    v = _view_params(query)
    if not schema_text:
        schema_text = schema_to_text(DEFAULT_SCHEMA)
    err_html = ""
    if errors:
        err_html = '<div class="alert error"><b>抽取失败：</b><br>' + "<br>".join(
            _esc(e) for e in errors
        ) + "</div>"
    if notice:
        err_html += f'<div class="alert ok"><b>{_esc(notice)}</b>'
        if job_id:
            err_html += f' · <a href="/job/{_esc(job_id)}/status">查看任务状态 JSON</a>'
        err_html += "</div>"
    meta_html = ""
    if meta:
        meta_html = (
            f'<div class="alert {"warn" if meta.get("degraded") else "info"}">'
            f'抽取来源：{_esc(meta.get("provider", "批量"))} / {_esc(meta.get("mode", ""))} ｜ '
            f'页数/份数：{_esc(_fmt_num(meta.get("pages", 1)))} ｜ '
            f'耗时：{_esc(_fmt_num(meta.get("elapsed", 0)))} 秒</div>'
        )
    local_options = ['<option value="">（不选择，用上传或路径）</option>']
    for rel in list_local_documents():
        local_options.append(f'<option value="{_esc(rel)}">{_esc(rel)}</option>')
    chosen = set(export_formats or ["csv", "xlsx"])
    export_checks = "".join(
        f'<label><input type="checkbox" name="export_formats" value="{fmt}"'
        f'{" checked" if fmt in chosen else ""}> .{fmt}</label>'
        for fmt in ("csv", "xlsx")
    )
    eco_summary = _eco_summary_html(meta)
    if batch and (batch.get("meta") or {}).get("eco_original_chars"):
        eco_summary += (
            '<div class="alert info">整批节能统计见下方「批量处理」区块。</div>'
        )
    result_json = (
        f'<details class="fold"><summary>完整结构化 JSON</summary>'
        f'<div><pre class="code-block">{_json_highlight(result)}</pre></div></details>'
        if result else ""
    )
    art_html = (
        _artifacts_html(run_id, artifacts or [])
        if artifacts else '<div class="alert info">执行抽取后，这里会出现 JSON / CSV / Excel 下载入口。</div>'
    )
    pipeline_link = ""
    csv_artifact = next(
        (a for a in (artifacts or []) if str(a.get("name", "")).endswith(".csv")),
        None,
    )
    if run_id and csv_artifact:
        csv_path = str(Path(base_output) / run_id / csv_artifact["name"])
        label = "批量抽取汇总" if batch else (
            "抽取表格" if (result or {}).get("tables") else "抽取字段"
        )
        query_str = urlencode({
            "source_path": csv_path,
            "source_format": "csv",
            "intent": "对抽取结果去重、填充缺失并整理，导出 csv",
        })
        pipeline_link = (
            '<div style="margin-top:14px">'
            f'<a class="btn btn-ghost" href="/?{query_str}">'
            f'将{label}送入清洗管道</a>'
            f'<span class="hint"> 以 {_esc(csv_artifact["name"])} 为数据源，回到工作台生成规则</span>'
            '</div>'
        )
    single = result if not batch else None
    body = render(
        "extract.html",
        BODY_CLASS=_body_class(v), NAV=_nav("extract"),
        SUPPORTED="PDF / DOCX / TXT / 图片",
        DOC_PATH=_esc(doc_path), DOC_SAMPLES=_doc_samples(),
        LOCAL_DOC_OPTIONS="".join(local_options),
        SCHEMA_TEXT=_esc(schema_text), ERRORS=err_html, META=meta_html,
        ECO_OPEN="open" if eco else "", ECO_CHECKED="checked" if eco else "",
        ECO_WINDOW=int(eco_window or ECO_DEFAULTS["window"]),
        ECO_MAX_SNIPPETS=int(eco_max_snippets or ECO_DEFAULTS["max_snippets"]),
        ECO_MAX_CHARS=int(eco_max_chars or ECO_DEFAULTS["max_chars"]),
        WORKERS=int(workers or ECO_DEFAULTS["max_workers"]),
        EXPORT_CHECKS=export_checks,
        MODE_SYNC_CHECKED="" if mode == "async" else "checked",
        MODE_ASYNC_CHECKED="checked" if mode == "async" else "",
        ECO_SUMMARY=eco_summary, BATCH_SUMMARY=_batch_summary_html(batch),
        RESULT_FIELDS=_fields_html(single or {}) if single else "",
        RESULT_TABLES=_tables_html(single or {}) if single else "",
        EVIDENCE=_evidence_html(single or {}) if single else "",
        RESULT_JSON=result_json,
        ARTIFACTS=art_html, PIPELINE_LINK=pipeline_link,
        CONTRACT=CONTRACT_EXTRACT,
        STEP2="active" if (result or batch) else "",
        STEP3="active" if (result or batch) else "",
        STEP4="active" if artifacts else "",
        FOOTER=_footer(), CONTROLS=_controls("/extract", v),
    )
    return Reply(body=body)


# ============================================================
# 后台任务页：等待 / 进度 / 结果取回
# ============================================================
_JOB_STATUS = {
    "queued": ("排队中", "info"),
    "running": ("执行中", "info"),
    "done": ("已完成", "ok"),
    "failed": ("失败", "error"),
}


def _job_badge(status: str) -> str:
    label, cls = _JOB_STATUS.get(status, (status, "info"))
    return f'<span class="job-badge {cls}">{_esc(label)}</span>'


def _job_bar(progress: float, status: str = "") -> str:
    pct = round(min(1.0, max(0.0, float(progress or 0))) * 100, 1)
    cls = "done" if status == "done" else ("failed" if status == "failed" else "")
    return (
        f'<div class="job-track"><div class="job-fill {cls}" style="width:{pct}%"></div></div>'
        f'<div class="job-pct">{_fmt_num(pct)}%</div>'
    )


def _job_page(query: Dict[str, List[str]], job: Dict[str, Any]) -> Reply:
    """等待状态页：进度条 + 阶段文案 + 自动刷新（不依赖 JS）。"""
    from jobs import ACTIVE

    v = _view_params(query)
    status = job["status"]
    label, cls = _JOB_STATUS.get(status, (status, "info"))
    refresh = (
        '<meta http-equiv="refresh" content="2">' if status in ACTIVE else ""
    )
    status_html = (
        f'<div class="alert {cls}"><b>当前状态：{_esc(label)}</b> · '
        f'{_esc(job["message"])}</div>'
    )
    if status == "failed":
        status_html += (
            '<div class="alert error"><b>任务失败：</b><br>'
            + _esc(job.get("error") or "未知错误") + "</div>"
        )
    progress_html = _job_bar(job["progress"], status)
    detail = (
        '<div class="job-meta">'
        f'<div><span>任务编号</span><b>{_esc(job["id"])}</b></div>'
        f'<div><span>任务类型</span><b>{_esc(job["kind"])}</b></div>'
        f'<div><span>处理方式</span><b>{_esc(job["meta"].get("mode", "异步"))}</b></div>'
        f'<div><span>已完成 / 总数</span><b>{_fmt_num(job["current"])} / {_fmt_num(job["total"])}</b></div>'
        f'<div><span>已耗时</span><b>{_fmt_num(job["elapsed"])} 秒</b></div>'
        f'<div><span>文档数</span><b>{_fmt_num(job["meta"].get("documents", 1))}</b></div>'
        "</div>"
    )
    back = "/extract" if job["kind"] == "extract" else "/"
    actions = "".join([
        f'<a class="btn btn-primary" href="/job/{_esc(job["id"])}">立即刷新</a>',
        '<a class="btn btn-ghost" href="/jobs">全部任务</a>',
        f'<a class="btn btn-ghost" href="{back}">返回{"文档抽取" if job["kind"] == "extract" else "工作台"}</a>',
        f'<a class="btn btn-ghost" href="/job/{_esc(job["id"])}/status">状态 JSON</a>',
    ])
    recent = JOBS.list_jobs(6)
    recent_rows = "".join(
        f'<tr><td><a href="/job/{_esc(j["id"])}">{_esc(j["label"])}</a></td>'
        f'<td>{_job_badge(j["status"])}</td>'
        f'<td>{_fmt_num(round(j["progress"] * 100, 1))}%</td>'
        f'<td>{_fmt_num(j["elapsed"])} 秒</td></tr>'
        for j in recent
    )
    recent_html = (
        '<div class="card" style="margin-top:18px"><h2>最近任务</h2>'
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        '<th>任务</th><th>状态</th><th>进度</th><th>耗时</th>'
        '</tr></thead><tbody>' + recent_rows + "</tbody></table></div></div>"
        if recent_rows else ""
    )
    body = render(
        "job.html",
        BODY_CLASS=_body_class(v), NAV=_nav("jobs"),
        META_REFRESH=refresh, TITLE=_esc(job["label"]),
        STATUS=status_html, PROGRESS=progress_html, DETAIL=detail,
        ACTIONS=actions, RECENT=recent_html,
        FOOTER=_footer(), CONTROLS=_controls(f"/job/{job['id']}", v),
    )
    return Reply(body=body)


def _jobs_page(query: Dict[str, List[str]]) -> Reply:
    """任务列表：异步任务可随时找回，页面在有活动任务时低频自动刷新。"""
    from jobs import ACTIVE

    v = _view_params(query)
    jobs = JOBS.list_jobs(30)
    rows = []
    for j in jobs:
        rows.append(
            f'<tr><td><a href="/job/{_esc(j["id"])}">{_esc(j["label"])}</a>'
            f'<div class="hint">{_esc(j["id"])} · {_esc(j["kind"])}</div></td>'
            f'<td>{_job_badge(j["status"])}</td>'
            f'<td>{_esc(j["message"])}</td>'
            f'<td>{_job_bar(j["progress"], j["status"])}</td>'
            f'<td>{_fmt_num(j["elapsed"])} 秒</td></tr>'
        )
    table = (
        '<div class="table-scroll"><table class="data-table"><thead><tr>'
        '<th>任务</th><th>状态</th><th>当前阶段</th><th>进度</th><th>耗时</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
        if rows else '<div class="alert info">还没有后台任务。在文档抽取页选择「异步」并提交即可创建。</div>'
    )
    refresh = (
        '<meta http-equiv="refresh" content="8">'
        if any(j["status"] in ACTIVE for j in jobs) else ""
    )
    body = render(
        "jobs.html", BODY_CLASS=_body_class(v), NAV=_nav("jobs"),
        META_REFRESH=refresh, JOBS_TABLE=table,
        FOOTER=_footer(), CONTROLS=_controls("/jobs", v),
    )
    return Reply(body=body)


def _job_view(
    query: Dict[str, List[str]], job_id: str, state: AppState
) -> Reply:
    """任务完成则交付最终结果，否则渲染等待页。"""
    from jobs import ACTIVE

    snap = JOBS.snapshot(job_id)
    if snap is None:
        return Reply(status=404, body="<h1>404 任务不存在或已被清理</h1>")
    if snap["status"] in ACTIVE or snap["status"] == "failed":
        return _job_page(query, snap)
    payload = JOBS.result(job_id)
    if snap["kind"] == "extract" and isinstance(payload, dict):
        kwargs = dict(payload)
        kwargs.setdefault("mode", "async")
        kwargs["notice"] = (
            f"异步任务已完成：{snap['label']}（耗时 {_fmt_num(snap['elapsed'])} 秒）"
        )
        kwargs["job_id"] = job_id
        try:
            return _extract_page(query, state.base_output, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 结果渲染失败时给出可读状态页
            LOGGER.warning("异步抽取结果渲染失败：%s", type(exc).__name__)
            snap["message"] = f"结果渲染失败：{exc}"
            return _job_page(query, snap)
    if snap["kind"] == "rule" and isinstance(payload, dict):
        location = str(payload.get("redirect") or "")
        if location.startswith("/result/"):
            return Reply(status=302, body="", headers={"Location": location})
        if payload.get("html"):
            return Reply(body=str(payload["html"]))
    return _job_page(query, snap)


# ============================================================
# 路由（Flask 与 http.server 共用）
# ============================================================
class AppState:
    def __init__(self, base_output: str) -> None:
        self.base_output = base_output


def _form_value(form: Dict[str, List[str]], key: str, default: str = "") -> str:
    vals = form.get(key)
    return vals[0] if vals else default


def _form_list(form: Dict[str, List[str]], key: str) -> List[str]:
    return form.get(key) or []


def handle_request(
    method: str, path: str,
    query: Dict[str, List[str]], form: Dict[str, List[str]],
    state: AppState,
    files: Optional[Dict[str, List[Tuple[str, bytes]]]] = None,
) -> Reply:
    """统一请求分派，返回 Reply。files 为 {字段名: [(文件名, 字节)]}。"""
    # 静态样式
    if path == "/static/style.css":
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        return Reply(body=css, content_type="text/css; charset=utf-8")

    # 后台任务：状态 JSON / 进度页 / 结果取回 / 任务列表
    m = re.match(r"^/job/([^/]+)/status$", path)
    if m:
        snap = JOBS.snapshot(m.group(1))
        if snap is None:
            return Reply(status=404, body='{"error":"job not found"}',
                         content_type="application/json; charset=utf-8")
        return Reply(body=json.dumps(snap, ensure_ascii=False, indent=2),
                     content_type="application/json; charset=utf-8")
    m = re.match(r"^/(?:job|extract/job)/([^/]+)$", path)
    if m:
        return _job_view(query, m.group(1), state)
    if path == "/jobs":
        return _jobs_page(query)

    # 下载（含报告内联查看）
    m = re.match(r"^/download/([^/]+)/([^/]+)$", path)
    if m:
        run_id, name = m.group(1), m.group(2)
        target = resolve_artifact(run_id, name, state.base_output)
        if target is None:
            return Reply(status=404, body="文件不存在或不在白名单内")
        is_html = target.suffix.lower() == ".html"
        ctype = "text/html; charset=utf-8" if is_html else "application/octet-stream"
        headers = {}
        if not is_html:
            headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
        return Reply(body=target.read_bytes(), content_type=ctype, headers=headers)

    # 结果页
    m = re.match(r"^/result/([^/]+)$", path)
    if m:
        return _result_page(query, m.group(1), state.base_output)

    # 运行记录列表（导航「结果查看」入口）
    if path == "/workbench-runs":
        runs = list_runs(state.base_output)
        if runs:
            return Reply(
                status=302, body="", headers={"Location": f"/result/{runs[0]['run_id']}"}
            )
        return Reply(
            status=302, body="",
            headers={"Location": "/?notes=1"},
        )

    if path in ("/", "/workbench") and method == "GET":
        return _workbench(
            query, state.base_output,
            intent=_form_value(query, "intent"),
            source_path=_form_value(query, "source_path", "lots.csv"),
            source_format=_form_value(query, "source_format", "csv"),
            column_aliases=_form_value(query, "column_aliases"),
        )

    # ------------------------------------------------------------
    # 规则库：保存 / 载入 / 直接执行 / 删除
    # ------------------------------------------------------------
    if path == "/rules/save" and method == "POST":
        raw = _form_value(form, "rule_json").strip()
        name = _form_value(form, "rule_name").strip()
        errors: List[str] = []
        rule_obj: Optional[Dict[str, Any]] = None
        try:
            from rule_store import save_rule as _save

            rule_obj = json.loads(raw)
            if not isinstance(rule_obj, dict):
                raise ValueError("规则 JSON 必须是对象")
            saved = _save(name, rule_obj)
            meta_saved = {
                "provider": "rule-store", "mode": "online", "model": "",
                "degraded": False, "elapsed": 0.0,
                "reason": f"已保存到规则库：{saved.name}（id={saved.id}），"
                          f"后续可在规则库直接选择执行",
            }
        except Exception as exc:  # noqa: BLE001
            errors = [f"规则保存失败：{exc}"]
            meta_saved = None
        return _workbench(
            query, state.base_output,
            intent=_form_value(form, "intent"),
            source_path=_form_value(form, "source_path", "lots.csv"),
            columns=_form_value(form, "columns"),
            rule=rule_obj, meta=meta_saved, errors=errors,
            rule_name=name,
        )

    if path == "/rules/load" and method == "GET":
        from rule_store import get_rule

        rid = _form_value(query, "id")
        item = get_rule(rid)
        if item is None:
            return _workbench(query, state.base_output, errors=[f"规则不存在：{rid}"])
        return _workbench(
            query, state.base_output,
            intent=item.intent,
            source_path=(item.rule.get("source") or {}).get("path", "lots.csv"),
            source_format=(item.rule.get("source") or {}).get("format", "csv"),
            rule=item.rule, rule_input_json=json.dumps(item.rule, ensure_ascii=False),
            rule_name=item.name, saved_rule_id=item.id,
            formats=(item.rule.get("output") or {}).get("formats"),
            meta={"provider": "rule-store", "mode": "offline", "model": "",
                  "degraded": False, "elapsed": 0.0,
                  "reason": f"已从规则库载入：{item.name}"},
        )

    if path == "/rules/delete" and method == "POST":
        from rule_store import delete_rule

        rid = _form_value(form, "rule_id")
        try:
            delete_rule(rid)
        except Exception as exc:  # noqa: BLE001
            return _workbench(query, state.base_output, errors=[f"删除失败：{exc}"])
        return Reply(status=302, body="", headers={"Location": "/?notes=1"})

    if path == "/rules/execute" and method == "POST":
        from rule_store import get_rule

        rid = _form_value(form, "rule_id")
        item = get_rule(rid)
        if item is None:
            return _workbench(query, state.base_output, errors=[f"规则不存在：{rid}"])
        try:
            from rule_executor import execute_rule
            from rules import assert_valid_rule, probe_columns

            src_map = item.rule.get("sources") if isinstance(item.rule.get("sources"), dict) else {}
            if not src_map:
                src_map = {"main": item.rule.get("source", {})}
            cols: List[str] = []
            for src in src_map.values():
                if isinstance(src, dict):
                    cols += probe_columns(src)
            assert_valid_rule(item.rule, list(dict.fromkeys(cols)))
            summary = execute_rule(
                item.rule, base_output=state.base_output,
                provider_info={"provider": "rule-store", "mode": "offline",
                               "degraded": False, "reason": f"规则库：{item.name}"},
            )
            return Reply(status=302, body="",
                         headers={"Location": f"/result/{summary['run_id']}"})
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("规则库执行失败: %s", type(exc).__name__)
            return _workbench(
                query, state.base_output, rule=item.rule,
                errors=[f"规则执行失败：{exc}"], rule_name=item.name,
                saved_rule_id=item.id,
            )

    # 生成规则 / 生成并立即执行
    if path in ("/plan", "/run") and method == "POST":
        intent = _form_value(form, "intent").strip()
        source_path = _form_value(form, "source_path", "lots.csv").strip()
        source_format = _form_value(form, "source_format", "csv")
        columns = _form_value(form, "columns").strip()
        alias_text = _form_value(form, "column_aliases").strip()
        rule_input = _form_value(form, "rule_json").strip()
        saved_rule_id = _form_value(form, "saved_rule_id").strip()
        rule_name = _form_value(form, "rule_name").strip()
        schema_mode = _form_value(form, "schema_mode").strip() == "1"
        formats = _form_list(form, "formats") or ["csv"]
        wants_report = "1" in _form_list(form, "report")
        if not intent:
            return _workbench(
                query, state.base_output, errors=["需求描述不能为空"],
                source_path=source_path, source_format=source_format,
                column_aliases=alias_text,
            )
        sources: Dict[str, Any] = {}
        try:
            from rules import (
                generate_rules, generate_rules_from_schema,
                parse_column_aliases, probe_columns, validate_rule,
            )

            sources = _collect_sources(form, files)
            source = sources["main"]
            cols = [c.strip() for c in columns.split(",") if c.strip()]
            if not cols:
                cols = probe_columns(source)
            multi = len(sources) > 1
            source_columns = {
                alias: probe_columns(src)
                for alias, src in sources.items()
            } if multi else None
            if saved_rule_id:
                # 直接选择规则库中已保存的规则：跳过 LLM，并按规则自身的
                # source 重新探测列名，避免用当前表单的数据源去校验别人的规则
                from rule_store import get_rule

                item = get_rule(saved_rule_id)
                if item is None:
                    raise ValueError(f"规则库中找不到 {saved_rule_id!r}")
                rule = json.loads(json.dumps(item.rule, ensure_ascii=False))
                meta = {
                    "provider": "rule-store", "mode": "offline", "model": "",
                    "degraded": False, "elapsed": 0.0,
                    "reason": f"已选择规则库规则：{item.name}",
                }
                src_map = rule.get("sources") if isinstance(rule.get("sources"), dict) else {}
                if not src_map:
                    src_map = {"main": rule.get("source", {})}
                probed: List[str] = []
                for src in src_map.values():
                    if isinstance(src, dict) and src.get("path"):
                        try:
                            probed += probe_columns(src)
                        except Exception:  # noqa: BLE001 - 源不可用时不阻塞
                            continue
                cols = list(dict.fromkeys(probed)) or cols
                source_columns = None
            elif rule_input:
                # 显式规则 JSON：不依赖 LLM，替代已移除的关键词 Mock 模板
                rule = json.loads(rule_input)
                if not isinstance(rule, dict):
                    raise ValueError("规则 JSON 必须是对象")
                alias_map, alias_warns = parse_column_aliases(alias_text, cols)
                meta = {
                    "provider": "manual", "mode": "offline", "model": "",
                    "degraded": False, "elapsed": 0.0,
                    "column_aliases": alias_map,
                    "alias_warnings": alias_warns,
                }
                if alias_map:
                    rule["column_aliases"] = dict(alias_map)
            elif schema_mode:
                # 结构化意图 Schema：真实 LLM 解析 → 确定性编译，不经中文正则兜底
                rule, meta = generate_rules_from_schema(
                    intent, source, cols,
                    sources=sources if multi else None,
                    source_columns=source_columns,
                    column_aliases=alias_text,
                )
            else:
                rule, meta = generate_rules(
                    intent, source, cols,
                    sources=sources if multi else None,
                    source_columns=source_columns,
                    column_aliases=alias_text,
                )
            rule["output"] = {"formats": formats, "report": wants_report}
            all_cols = list(dict.fromkeys(
                cols + [c for cl in (source_columns or {}).values() for c in cl]
            ))
            errs = validate_rule(rule, all_cols)
            if errs:
                return _workbench(
                    query, state.base_output, intent=intent,
                    source_path=source_path, source_format=source_format,
                    columns=columns, formats=formats, errors=errs,
                    sources=sources, column_aliases=alias_text,
                    rule_input_json=rule_input,
                saved_rule_id=saved_rule_id, rule_name=rule_name,
                schema_mode=schema_mode,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Web 规则生成失败: %s", type(exc).__name__)
            return _workbench(
                query, state.base_output, intent=intent,
                source_path=source_path, source_format=source_format,
                columns=columns, formats=formats, errors=[str(exc)],
                sources=sources, column_aliases=alias_text,
                rule_input_json=rule_input,
                schema_mode=schema_mode,
            )
        if path == "/plan":
            return _workbench(
                query, state.base_output, intent=intent,
                source_path=source_path, source_format=source_format,
                columns=columns, formats=formats, rule=rule, meta=meta,
                sources=sources, column_aliases=alias_text,
                rule_input_json=rule_input,
                saved_rule_id=saved_rule_id, rule_name=rule_name,
                schema_mode=schema_mode,
            )
        try:
            from rule_executor import execute_rule

            summary = execute_rule(
                rule, base_output=state.base_output, provider_info=meta
            )
            return Reply(
                status=302, body="",
                headers={"Location": f"/result/{summary['run_id']}"},
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Web 规则执行失败: %s", exc)
            return _workbench(
                query, state.base_output, intent=intent,
                source_path=source_path, source_format=source_format,
                columns=columns, formats=formats, rule=rule, meta=meta,
                errors=[f"执行失败：{exc}"], column_aliases=alias_text,
            )

    # 校验并执行编辑后的规则 JSON
    if path == "/execute" and method == "POST":
        raw = _form_value(form, "rule_json")
        try:
            from rules import assert_valid_rule, probe_columns
            from rule_executor import execute_rule

            rule = json.loads(raw)
            source_map = rule.get("sources") if isinstance(rule.get("sources"), dict) else {}
            if not source_map:
                source_map = {"main": rule.get("source", {})}
            all_cols: List[str] = []
            for src in source_map.values():
                if isinstance(src, dict):
                    all_cols += probe_columns(src)
            assert_valid_rule(rule, list(dict.fromkeys(all_cols)))
            summary = execute_rule(
                rule, base_output=state.base_output,
                provider_info={"provider": "manual", "mode": "offline", "degraded": False},
            )
            return Reply(
                status=302, body="",
                headers={"Location": f"/result/{summary['run_id']}"},
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Web 编辑规则执行失败: %s", type(exc).__name__)
            return _workbench(
                query, state.base_output,
                errors=[f"规则校验/执行失败：{exc}"],
            )

    # 文档抽取（单份 / 批量）
    if path == "/extract":
        if method == "GET":
            return _extract_page(
                query, state.base_output,
                doc_path=_form_value(query, "doc_path"),
            )
        from doc_extract import (
            ECO_DEFAULTS, extract_document, extract_documents_batch,
            parse_schema, save_document_upload,
        )
        from exporters import export_extraction_batch

        doc_path = _form_value(form, "doc_path").strip()
        schema_text = _form_value(form, "schema_text")
        eco = "1" in _form_list(form, "eco")
        eco_window = _form_value(form, "eco_window", str(ECO_DEFAULTS["window"]))
        eco_snippets = _form_value(form, "eco_max_snippets", str(ECO_DEFAULTS["max_snippets"]))
        eco_chars = _form_value(form, "eco_max_chars", str(ECO_DEFAULTS["max_chars"]))
        workers_raw = _form_value(form, "workers", str(ECO_DEFAULTS["max_workers"]))
        export_formats = _form_list(form, "export_formats") or ["csv"]
        mode = _form_value(form, "mode", "sync").strip().lower()
        if mode not in ("sync", "async"):
            mode = "sync"

        def _int(raw: str, default: int) -> int:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        cfg = {
            "window": _int(eco_window, ECO_DEFAULTS["window"]),
            "max_snippets": _int(eco_snippets, ECO_DEFAULTS["max_snippets"]),
            "max_chars": _int(eco_chars, ECO_DEFAULTS["max_chars"]),
            "max_workers": _int(workers_raw, ECO_DEFAULTS["max_workers"]),
        }
        common = dict(
            schema_text=schema_text, eco=eco,
            eco_window=cfg["window"], eco_max_snippets=cfg["max_snippets"],
            eco_max_chars=cfg["max_chars"], workers=cfg["max_workers"],
            export_formats=export_formats, mode=mode,
        )

        # 上传落盘 / schema 解析 / 路径解析都在请求线程完成（快且可即时报错），
        # 后台线程只做耗时的 LLM 抽取与导出，不持有 request 对象。
        try:
            schema = parse_schema(schema_text)
        except Exception as exc:  # noqa: BLE001
            return _extract_page(query, state.base_output, doc_path=doc_path,
                                 errors=[f"字段定义解析失败：{exc}"], **common)
        paths: List[str] = []
        upload_errors: List[str] = []
        for filename, data in (files or {}).get("doc_files") or []:
            if not data:
                continue
            try:
                paths.append(save_document_upload(filename, data))
            except Exception as exc:  # noqa: BLE001 - 单个上传失败不影响其余
                upload_errors.append(f"{filename}：{exc}")
        local = _form_value(form, "local_doc").strip()
        if local:
            candidate = Path(local) if Path(local).is_absolute() else ROOT / local
            if candidate.is_file():
                paths.append(str(candidate.resolve()))
            else:
                upload_errors.append(f"所选本地文件不存在：{local}")
        if doc_path:
            paths.append(doc_path)
        paths = list(dict.fromkeys(paths))  # 去重保序，避免同一文件重复计费
        if not paths:
            return _extract_page(
                query, state.base_output, doc_path=doc_path,
                errors=["请上传文档、从列表选择文件或填写文档路径"
                        + ("；" + "；".join(upload_errors) if upload_errors else "")],
                **common,
            )
        if upload_errors:
            LOGGER.warning("部分文档被跳过：%s", upload_errors)

        def _do_extract(progress=None) -> Dict[str, Any]:
            """同步/异步共用的执行体：返回 _extract_page 的关键字参数。"""
            report = progress or (lambda *_args: None)
            total = len(paths)
            report(0, total, "准备解析文档与字段定义")
            run_dir = new_run_dir(state.base_output)
            if total == 1:
                result, meta = extract_document(
                    paths[0], schema, eco=eco, eco_config=cfg,
                )
                report(1, 1, "抽取完成，正在导出结构化结果")
                artifacts = [a.to_dict() for a in
                             export_extraction(result, run_dir, formats=export_formats)]
                batch = None
            else:
                batch = extract_documents_batch(
                    paths, schema, eco=eco, eco_config=cfg,
                    max_workers=cfg["max_workers"],
                    on_progress=lambda done, count, name: report(
                        done, count, f"已完成 {done}/{count}：{name}"),
                )
                report(total, total, "批量抽取完成，正在导出汇总宽表")
                artifacts = [a.to_dict() for a in
                             export_extraction_batch(batch, run_dir, formats=export_formats)]
                meta = dict(batch["meta"])
                meta.update({"provider": "batch", "mode": "online",
                             "degraded": bool(batch["meta"].get("failed")),
                             "pages": batch["meta"].get("total", total)})
                result = None
            return dict(
                common, doc_path=doc_path, result=result, meta=meta,
                batch=batch, run_id=run_dir.name, artifacts=artifacts,
            )

        if mode == "async":
            label = (
                f"文档抽取 · {len(paths)} 份（{'节能' if eco else '全文'} / 异步）"
            )
            job_id = JOBS.submit(
                "extract", label, _do_extract, total=len(paths),
                meta={"mode": "异步", "documents": len(paths),
                      "eco": eco, "workers": cfg["max_workers"]},
            )
            LOGGER.info("文档抽取已转为异步任务 %s（%d 份）", job_id, len(paths))
            return Reply(status=302, body="",
                         headers={"Location": f"/job/{job_id}"})

        try:
            payload = _do_extract()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Web 文档抽取失败: %s", type(exc).__name__)
            return _extract_page(
                query, state.base_output, doc_path=doc_path,
                errors=[str(exc)], **common,
            )
        return _extract_page(query, state.base_output, **payload)

    return Reply(status=404, body="<h1>404 Not Found</h1>")


# ============================================================
# 两种服务端适配器
# ============================================================
def _reply_to_flask(reply: Reply):
    body = reply.body
    if isinstance(body, str):
        body = body.encode("utf-8")
    resp = FlaskResponse(body, status=reply.status, content_type=reply.content_type)
    for k, val in reply.headers.items():
        resp.headers[k] = val
    return resp


def _parse_multipart(
    content_type: str, raw: bytes
) -> Tuple[Dict[str, List[str]], Dict[str, List[Tuple[str, bytes]]]]:
    """用标准库手动解析 multipart/form-data（无 Flask 时的降级路径）。

    不依赖 ``email.parser``：浏览器对 ``Content-Transfer-Encoding: binary``
    或大文件的处理在不同实现下会让 ``email`` 模块丢字段甚至抛异常，导致
    ``intent`` 等文本字段解析为空（表现为「需求描述不能为空」）。这里按
    边界切片，逐个字段提取，单字段失败不影响其余字段。
    """
    form: Dict[str, List[str]] = {}
    files: Dict[str, List[Tuple[str, bytes]]] = {}
    m = re.search(r"boundary=([^;]+)", content_type, re.IGNORECASE)
    if not m:
        return form, files
    boundary = m.group(1).strip().strip('"').strip().encode("utf-8", "replace")
    if not boundary:
        return form, files
    # 按 "--boundary" 切分：parts[0] 为前导空段，parts[-1] 为结尾的 "--"
    parts = raw.split(b"--" + boundary)
    for part in parts[1:-1]:
        if not part:
            continue
        # 去掉紧跟边界之后的一个换行（CRLF 或 LF）
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        # 头与体之间以空行分隔；这里必须先定位空行，再处理体尾换行，
        # 否则空值字段（如空的 columns / 别名）会把分隔空行吃掉而被丢弃。
        if b"\r\n\r\n" in part:
            header_raw, body = part.split(b"\r\n\r\n", 1)
            trail = b"\r\n"
        elif b"\n\n" in part:
            header_raw, body = part.split(b"\n\n", 1)
            trail = b"\n"
        else:
            continue
        # 体尾的一个换行属于下一个边界，需剔除（空体时即空字符串）
        if body.endswith(trail):
            body = body[: -len(trail)]
        header = header_raw.decode("utf-8", "replace").replace("\r\n", "\n")
        name = filename = ""
        for line in header.split("\n"):
            if line.lower().startswith("content-disposition:"):
                name = _header_param(line, "name")
                filename = _header_param(line, "filename")
        if not name:
            continue
        if filename:
            files.setdefault(name, []).append((filename, body))
        else:
            form.setdefault(name, []).append(body.decode("utf-8", "replace"))
    return form, files


def _header_param(header_line: str, key: str) -> str:
    """从 Content-Disposition 等头中提取 name=\"...\" / filename=\"...\"。"""
    m = re.search(
        rf'{key}\s*=\s*("([^"]*)"|([^;]*))',
        header_line, re.IGNORECASE,
    )
    if not m:
        return ""
    return (m.group(2) if m.group(2) is not None else m.group(3) or "").strip()


def _make_flask(state: AppState):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
    @app.route("/<path:path>", methods=["GET", "POST"])
    def catch_all(path: str):
        full = "/" + path
        query = {k: v for k, v in request.args.lists()}
        form = {k: request.form.getlist(k) for k in request.form.keys()}
        files: Dict[str, List[Tuple[str, bytes]]] = {}
        try:
            for key in request.files:
                items = []
                for storage in request.files.getlist(key):
                    items.append((storage.filename or key, storage.read()))
                if items:
                    files[key] = items
        except Exception:  # noqa: BLE001 - 非 multipart 请求
            files = {}
        return _reply_to_flask(
            handle_request(request.method, full, query, form, state, files)
        )

    return app


class _StdHandler(BaseHTTPRequestHandler):
    state: AppState = AppState("./output")

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _serve(self, method: str) -> None:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        form: Dict[str, List[str]] = {}
        files: Dict[str, List[Tuple[str, bytes]]] = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > MAX_BODY_BYTES:
                LOGGER.warning("拒绝超大请求：Content-Length=%d", length)
                self.send_response(413)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if content_type.lower().startswith("multipart/form-data"):
                form, files = _parse_multipart(content_type, raw)
            else:
                form = parse_qs(
                    raw.decode("utf-8", errors="replace"), keep_blank_values=True
                )
        reply = handle_request(method, path, query, form, self.state, files)
        body = reply.body
        payload = body.encode("utf-8") if isinstance(body, str) else (body or b"")
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(payload)))
        for k, val in reply.headers.items():
            self.send_header(k, val)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")


def run_server(host: str = "127.0.0.1", port: int = 8765,
               base_output: str = "./output") -> int:
    """启动本地服务并返回退出码。Flask 缺失时自动降级标准库实现。"""
    _ensure_samples()
    state = AppState(base_output)
    mode = "Flask" if Flask is not None else "http.server(标准库降级)"
    LOGGER.info("Web 工作台启动: http://%s:%d (%s) 产物目录=%s",
                host, port, mode, base_output)
    print(f"DataClean Console: http://{host}:{port}  [{mode}]")
    print("按 Ctrl+C 停止服务")
    try:
        if Flask is not None:
            _make_flask(state).run(host=host, port=port, debug=False, threaded=True)
        else:
            _StdHandler.state = state
            ThreadingHTTPServer((host, port), _StdHandler).serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Web 服务已停止")
    return 0


def _ensure_samples() -> None:
    """创建两个可离线演示的样例文档（幂等）。"""
    SAMPLES.mkdir(exist_ok=True)
    contract = SAMPLES / "contract.txt"
    if not contract.exists():
        contract.write_text(
            "合同编号：HT-2026-0088\n"
            "主体名称：福州某某置业有限公司\n"
            "日期：2026-11-08\n"
            "金额：2400000\n"
            "标的地址：福州市晋安区王庄街道\n",
            encoding="utf-8",
        )
    table = SAMPLES / "table.txt"
    if not table.exists():
        table.write_text(
            "项目清单\n"
            "| 项目 | 数量 | 单价 |\n"
            "| 住宅 | 2 | 1200000 |\n"
            "| 车位 | 1 | 80000 |\n"
            "| 商铺 | 1 | 3500000 |\n",
            encoding="utf-8",
        )

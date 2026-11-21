#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可插拔 LLM Provider：OpenAI 兼容在线实现（不提供关键词 Mock 模板）。

设计要点
--------
- 上层模块（rules / doc_extract / webapp）只依赖 ``LLMProvider`` 协议，
  不感知具体厂商，便于替换或扩展。
- 工厂按「显式参数 -> 系统环境变量 -> 项目根 .env」选择实现；
  ``LLM_API_KEY`` 为空时抛出配置错误，由上层提示配置或改用显式规则 JSON。
- HTTP 优先 ``requests``，缺失时自动使用标准库 ``urllib``，因此
  未安装任何可选依赖也能接入在线 LLM；离线场景请使用显式规则 JSON（--rule-file）。
- ``.env`` 由内置轻量解析器读取（零第三方依赖），显式导出的系统环境变量
  优先级更高；文件修改后无需重启进程即可生效。
- 安全：日志只记录模型、耗时、响应长度与失败原因，**绝不记录**
  API Key、文档全文或 LLM 完整响应内容。

环境变量
--------
LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_VISION_MODEL /
LLM_TIMEOUT / LLM_MAX_RETRIES / LLM_PROVIDER
"""
from __future__ import annotations

import base64
import hashlib
import json
import ast
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

LOGGER = logging.getLogger("etl")

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - 取决于运行环境
    requests = None  # type: ignore


# ============================================================
# 错误归一化
# ============================================================
class LLMError(RuntimeError):
    """LLM 调用失败的统一异常，kind 便于上层决定重试或报错策略。"""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


# ============================================================
# Provider 协议
# ============================================================
class LLMProvider(Protocol):
    """所有 Provider 必须实现的契约。"""

    name: str
    model: str

    def complete(self, system: str, user: str) -> str:
        """文本任务，返回原始文本响应（通常为 JSON 字符串）。"""
        ...

    def complete_vision(
        self, system: str, user: str, images: List[Tuple[str, bytes]]
    ) -> str:
        """多模态任务；images 为 (mime_type, 图片字节) 列表。"""
        ...


@dataclass
class ProviderInfo:
    """供 Web 状态徽章与执行摘要使用的 Provider 描述。"""

    name: str
    mode: str          # online | offline
    model: str
    reason: str = ""


def describe_provider(provider: LLMProvider) -> ProviderInfo:
    reason = getattr(provider, "reason", "")
    return ProviderInfo(
        name=provider.name,
        mode=str(getattr(provider, "mode", "online")),
        model=getattr(provider, "model", "") or "",
        reason=reason,
    )


# ============================================================
# 通用工具：LLM 输出 JSON 解析修复
# ============================================================
def _strip_json_noise(raw: str) -> str:
    """清理模型常见的 JSON 噪声：注释、尾逗号、弯引号、零宽字符、截断括号。

    用状态机跳过字符串内容，避免误改值里的 ``//`` 或 ``, }``。
    """
    text = raw.replace("\ufeff", "")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    for source, target in (("“", '"'), ("”", '"'), ("„", '"'), ("‘", "'"), ("’", "'")):
        text = text.replace(source, target)

    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            out.append(char)
            index += 1
            while index < length:
                nxt = text[index]
                out.append(nxt)
                if nxt == "\\" and index + 1 < length:
                    out.append(text[index + 1])
                    index += 2
                    continue
                index += 1
                if nxt == '"':
                    break
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                text[index] == "*" and text[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    cleaned = "".join(out)

    # 尾逗号：在字符串外把 ",}" / ",]" 的逗号去掉
    result: List[str] = []
    in_string = False
    escaped = False
    for pos, char in enumerate(cleaned):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
            continue
        if char == ",":
            tail = cleaned[pos + 1:].lstrip()
            if tail.startswith(("}", "]")):
                continue
        result.append(char)

    repaired = "".join(result)
    # 截断修复：补齐未闭合的括号/方括号
    stack: List[str] = []
    in_string = False
    escaped = False
    for char in repaired:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if stack and not in_string:
        repaired += "".join("}" if c == "{" else "]" for c in reversed(stack))
    return repaired


def parse_json_response(text: str) -> Any:
    """从 LLM 响应中提取 JSON，兼容代码围栏、解释文字与常见格式噪声。

    依次尝试：原样解析 → 截取 JSON 主体 → 清洗注释/尾逗号/弯引号/截断括号
    → ``ast.literal_eval``（单引号 Python 字典）。全部失败时抛
    LLMError(kind="parse")，由上层决定回灌重试或报错。
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMError("LLM 返回空响应", "parse")
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw[3:]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
        raw = raw.rsplit("```", 1)[0]

    candidates: List[str] = [raw]
    starts = [p for p in (raw.find("{"), raw.find("[")) if p >= 0]
    if starts:
        start = min(starts)
        closing = "}" if raw[start] == "{" else "]"
        end = raw.rfind(closing)
        if end > start:
            candidates.append(raw[start : end + 1])
    for candidate in list(candidates):
        cleaned = _strip_json_noise(candidate)
        if cleaned not in candidates:
            candidates.append(cleaned)

    first_error: Optional[json.JSONDecodeError] = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
    for candidate in candidates:
        try:
            value = ast.literal_eval(candidate)
            if isinstance(value, (dict, list)):
                return value
        except (ValueError, SyntaxError):
            continue
    if first_error is not None:
        raise LLMError(f"LLM 响应 JSON 解析失败: {first_error}", "parse") from first_error
    raise LLMError("LLM 响应中未找到合法 JSON", "parse")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ============================================================
# .env 配置加载（标准库实现，无需 python-dotenv）
# ============================================================
_DOTENV_KEYS = (
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_VISION_MODEL",
    "LLM_TIMEOUT", "LLM_MAX_RETRIES", "LLM_PROVIDER",
    "OPENAI_BASE_URL", "OPENAI_API_KEY",
)
_DOTENV_INJECTED: Set[str] = set()
_DOTENV_HASH: Dict[str, str] = {}
_DOTENV_STATE: Dict[str, Any] = {"path": "", "mtime": -1.0}


def _hash_value(value: str) -> str:
    """只保存值的摘要，避免在模块状态里额外留存 API Key 明文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_dotenv(path: Optional[str] = None) -> Optional[Path]:
    """读取项目根 ``.env`` 中的 LLM 配置，返回实际加载的文件。

    优先级：显式导出的系统环境变量 > ``.env``。由 ``.env`` 注入的键允许
    随文件修改热更新；若某个键在运行期间被外部显式改写，则视为系统级
    覆盖，之后不再由 ``.env`` 改动。日志仅记录文件名与键数量，
    **绝不记录任何值**（尤其不记录 API Key）。
    """
    candidates = [Path(path)] if path else [
        Path(__file__).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]
    target = next((p for p in candidates if p.is_file()), None)
    if target is None:
        for key in list(_DOTENV_INJECTED):
            os.environ.pop(key, None)
            _DOTENV_INJECTED.discard(key)
        _DOTENV_HASH.clear()
        _DOTENV_STATE.update({"path": "", "mtime": -1.0})
        return None

    mtime = target.stat().st_mtime
    if _DOTENV_STATE["path"] == str(target) and _DOTENV_STATE["mtime"] == mtime:
        return target

    count = 0
    for line in target.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in _DOTENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in _DOTENV_INJECTED and _DOTENV_HASH.get(key) != _hash_value(
            os.environ.get(key, "")
        ):
            # 外部显式改写过该键：放弃 .env 对该键的所有权
            _DOTENV_INJECTED.discard(key)
            _DOTENV_HASH.pop(key, None)
            continue
        if key not in os.environ or key in _DOTENV_INJECTED:
            os.environ[key] = value
            _DOTENV_INJECTED.add(key)
            _DOTENV_HASH[key] = _hash_value(value)
        count += 1

    _DOTENV_STATE.update({"path": str(target), "mtime": mtime})
    LOGGER.info("已加载 .env 配置 文件=%s 键数=%d（不记录任何值）", target.name, count)
    return target


# ============================================================
# OpenAI 兼容 Provider
# ============================================================
class OpenAICompatProvider:
    """调用 OpenAI 兼容的 /chat/completions 端点（含多模态视觉消息）。"""

    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        vision_model: str = "",
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.vision_model = vision_model or model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.reason = ""

    @staticmethod
    def _post_json(
        url: str, headers: Dict[str, str],
        payload: Dict[str, Any], timeout: float,
    ) -> Tuple[int, Dict[str, Any]]:
        """POST JSON 并返回 (status_code, data)。

        ``requests`` 可用时优先使用；缺失时退回标准库 ``urllib``，
        因此无需安装任何可选依赖也能接入在线 LLM。
        """
        if requests is not None:
            resp = requests.post(
                url, headers=headers, json=payload, timeout=timeout
            )
            status = int(resp.status_code)
            return status, resp.json() if status < 400 else {}

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return int(resp.status), json.loads(raw)
        except urllib.error.HTTPError as exc:
            return int(exc.code), {}

    def _call(self, messages: List[Dict[str, Any]], model: str) -> str:
        url = (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else f"{self.base_url}/chat/completions"
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        last_kind = "network"
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                status, data = self._post_json(url, headers, payload, self.timeout)
                elapsed = time.monotonic() - started
                if status >= 500:
                    last_kind = "http"
                    LOGGER.warning(
                        "LLM 服务端错误 status=%s 耗时=%.2fs 第 %d 次",
                        status, elapsed, attempt + 1,
                    )
                    if attempt < self.max_retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise LLMError(f"LLM 服务端错误 HTTP {status}", "http")
                if status >= 400:
                    raise LLMError(f"LLM 请求被拒绝 HTTP {status}", "http")
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("LLM 响应缺少文本内容", "parse")
                LOGGER.info(
                    "LLM 调用完成 model=%s 耗时=%.2fs 响应长度=%d",
                    model, elapsed, len(content),
                )
                return content
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - 归一化第三方异常
                last_kind = "network"
                LOGGER.warning(
                    "LLM 网络异常 第 %d 次: %s", attempt + 1, type(exc).__name__
                )
                if attempt >= self.max_retries:
                    raise LLMError(
                        f"LLM 网络调用失败: {type(exc).__name__}", last_kind
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
        raise LLMError("LLM 调用重试耗尽", last_kind)

    def complete(self, system: str, user: str) -> str:
        return self._call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            self.model,
        )

    def complete_vision(
        self, system: str, user: str, images: List[Tuple[str, bytes]]
    ) -> str:
        content: List[Dict[str, Any]] = [{"type": "text", "text": user}]
        for mime, blob in images:
            b64 = base64.b64encode(blob).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
        return self._call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            self.vision_model,
        )


# ============================================================
# 工厂
# ============================================================
def get_provider(
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    vision_model: Optional[str] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> LLMProvider:
    """选择 Provider：显式参数 > 系统环境变量 > .env；未配置凭据时报错。"""
    load_dotenv()
    chosen = (provider or _env("LLM_PROVIDER")).lower()
    key = api_key if api_key is not None else (
        _env("LLM_API_KEY") or _env("OPENAI_API_KEY")
    )
    url = (
        base_url or _env("LLM_BASE_URL")
        or _env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    )
    mdl = model or _env("LLM_MODEL") or "gpt-4o-mini"
    vmdl = vision_model or _env("LLM_VISION_MODEL") or mdl
    try:
        tmo = float(timeout if timeout is not None else _env("LLM_TIMEOUT", "60"))
    except ValueError:
        tmo = 60.0
    try:
        retries = int(
            max_retries if max_retries is not None else _env("LLM_MAX_RETRIES", "2")
        )
    except ValueError:
        retries = 2

    if chosen == "mock":
        raise LLMError(
            "离线 Mock 模板已移除：请配置 LLM_API_KEY，或使用 --rule-file / 规则编辑器提供显式规则 JSON",
            "config",
        )
    if chosen not in ("", "openai", "openai-compat"):
        raise LLMError(f"未知 LLM_PROVIDER={chosen!r}，可选 openai-compat", "config")
    if not key:
        raise LLMError(
            "未配置 LLM_API_KEY（.env 或系统环境变量）；"
            "如需离线运行，请使用 --rule-file 提供显式规则 JSON",
            "config",
        )

    transport = "requests" if requests is not None else "urllib"
    LOGGER.info(
        "LLM Provider=openai-compat model=%s endpoint=%s transport=%s",
        mdl, url, transport,
    )
    return OpenAICompatProvider(url, key, mdl, vmdl, tmo, retries)


def diagnose_config() -> Dict[str, Any]:
    """返回可安全打印的配置诊断；**绝不返回 API Key 的值**。"""
    from urllib.parse import urlparse

    load_dotenv()
    try:
        info = describe_provider(get_provider())
    except LLMError as exc:
        info = ProviderInfo(name="unconfigured", mode="unconfigured", model="", reason=str(exc))
    endpoint = (
        _env("LLM_BASE_URL") or _env("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    key = _env("LLM_API_KEY") or _env("OPENAI_API_KEY")
    return {
        "mode": info.mode,
        "provider": info.name,
        "model": info.model,
        "reason": info.reason,
        "dotenv": _DOTENV_STATE.get("path", ""),
        "endpoint_host": urlparse(endpoint).netloc or endpoint,
        "api_key": f"已配置（长度 {len(key)}，值不输出）" if key else "未配置",
        "transport": "requests" if requests is not None else "urllib（标准库）",
        "provider_override": _env("LLM_PROVIDER") or "自动",
    }

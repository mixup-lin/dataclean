# -*- coding: utf-8 -*-
"""规则库：把生成/编辑过的规则 JSON 持久保存，后续按名称或 ID 直接复用。

设计约定
--------
- 每条规则一个独立 JSON 文件，存于 ``rules/``；不维护易损坏的全局索引，
  ``list_rules()`` 直接扫描目录，单条文件损坏不影响其余规则。
- ID 只由安全字符组成，读取前严格校验，杜绝路径穿越。
- 写入采用「临时文件 + 原子替换」，避免并发写产生半截 JSON。
- 规则内容原样保存（不改写 source / steps），元数据只用于检索与展示。
- 仅依赖标准库，CLI 与 Web 共用同一套 API。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("etl")

ROOT = Path(__file__).resolve().parent
RULES_DIR = ROOT / "rules"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_NAME = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class RuleStoreError(RuntimeError):
    """规则库读写/校验失败。"""


@dataclass
class SavedRule:
    """一条已保存规则及其检索元数据。"""

    id: str
    name: str
    intent: str = ""
    created_at: str = ""
    updated_at: str = ""
    task_type: str = ""
    op_count: int = 0
    source_path: str = ""
    rule: Dict[str, Any] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def steps(self) -> List[Dict[str, Any]]:
        return [s for s in (self.rule.get("steps") or []) if isinstance(s, dict)]


def _clean_name(name: Any) -> str:
    text = _CONTROL.sub(" ", str(name or "")).strip()
    if not text:
        raise RuleStoreError("规则名称不能为空")
    return text[:_MAX_NAME]


def _path_for(rule_id: str) -> Path:
    if not _ID_RE.match(str(rule_id or "")):
        raise RuleStoreError(f"非法规则 ID：{rule_id!r}")
    return RULES_DIR / f"{rule_id}.json"


def _read(path: Path) -> Optional[SavedRule]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("rule"), dict):
            return None
        item = SavedRule(
            id=str(payload.get("id") or path.stem),
            name=str(payload.get("name") or path.stem),
            intent=str(payload.get("intent") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            task_type=str(payload.get("task_type") or ""),
            op_count=int(payload.get("op_count") or 0),
            source_path=str(payload.get("source_path") or ""),
            rule=payload["rule"],
        )
        return item
    except (OSError, ValueError, TypeError) as exc:
        LOGGER.warning("跳过损坏的规则文件 %s：%s", path.name, exc)
        return None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_rule(
    name: str,
    rule: Dict[str, Any],
    *,
    rule_id: str = "",
    intent: str = "",
) -> SavedRule:
    """保存（或按 ``rule_id`` 覆盖更新）一条规则，返回带元数据的对象。

    同名重复保存会新建一条，便于保留不同版本；需要覆盖旧版请显式传 ``rule_id``。
    """
    if not isinstance(rule, dict):
        raise RuleStoreError("规则必须是 JSON 对象")
    clean = _clean_name(name)
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    rid = str(rule_id or "").strip()
    now = _now()
    if rid:
        target = _path_for(rid)
        existing = _read(target) if target.exists() else None
        created = existing.created_at if existing else now
    else:
        rid = f"r{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
        target = _path_for(rid)
        created = now

    steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    src = rule.get("source") if isinstance(rule.get("source"), dict) else {}
    item = SavedRule(
        id=rid, name=clean,
        intent=str(intent or rule.get("intent") or ""),
        created_at=created, updated_at=now,
        task_type=str(rule.get("task_type") or ""),
        op_count=len(steps),
        source_path=str(src.get("path") or ""),
        rule=rule,
    )
    payload = json.dumps(item.to_dict(), ensure_ascii=False, indent=2)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)  # 原子替换，避免并发读到半截文件
    LOGGER.info("规则已保存 id=%s name=%s 算子数=%d", rid, clean, len(steps))
    return item


def list_rules() -> List[SavedRule]:
    """按更新时间倒序列出全部规则；单条损坏自动跳过。"""
    if not RULES_DIR.exists():
        return []
    items = [
        entry for p in sorted(RULES_DIR.glob("*.json"))
        if (entry := _read(p)) is not None
    ]
    items.sort(key=lambda x: x.updated_at, reverse=True)
    return items


def get_rule(rule_id: str) -> Optional[SavedRule]:
    path = _path_for(rule_id)
    return _read(path) if path.exists() else None


def resolve_rule(ref: str) -> Optional[SavedRule]:
    """按 ID 或名称解析规则（CLI 用 ``--rule 名称`` 时更友好）。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    direct = get_rule(ref) if _ID_RE.match(ref) else None
    if direct:
        return direct
    for item in list_rules():
        if item.name == ref or item.id == ref:
            return item
    return None


def delete_rule(rule_id: str) -> bool:
    path = _path_for(rule_id)
    if not path.exists():
        return False
    path.unlink()
    LOGGER.info("规则已删除 id=%s", rule_id)
    return True


def load_rule_dict(ref: str) -> Dict[str, Any]:
    """按 ID / 名称取回可直接执行的规则 JSON；找不到时抛出可读异常。"""
    item = resolve_rule(ref)
    if item is None:
        raise RuleStoreError(
            f"规则库中找不到 {ref!r}；可用 `etl_pipeline.py rules list` 查看已保存规则"
        )
    return json.loads(json.dumps(item.rule, ensure_ascii=False))


__all__ = [
    "RuleStoreError", "SavedRule", "RULES_DIR",
    "save_rule", "list_rules", "get_rule", "resolve_rule",
    "delete_rule", "load_rule_dict",
]

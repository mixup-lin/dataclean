# -*- coding: utf-8 -*-
"""测试专用 Provider：只回显式给定的规则/抽取 JSON，不含任何关键词猜测。

产品代码已移除 Mock 模板；测试如需离线跑通链路，必须自行提供确定的规则数据。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


class FixtureProvider:
    """实现 LLMProvider 协议，但响应完全由用例注入。"""

    name = "fixture"
    mode = "offline"
    model = "fixture-v1"
    reason = "pytest 显式规则"

    def __init__(
        self,
        rule: Optional[Dict[str, Any]] = None,
        extraction: Optional[Dict[str, Any]] = None,
        vision: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.rule = rule
        self.extraction = extraction or {"fields": {}, "tables": [], "confidence": {}, "evidence": []}
        self.vision = vision or self.extraction

    def complete(self, system: str, user: str) -> str:
        # 未注入规则时按「抽取任务」返回显式结构化结果
        payload = self.rule if self.rule is not None else self.extraction
        return json.dumps(payload, ensure_ascii=False)

    def complete_vision(
        self, system: str, user: str, images: List[Tuple[str, bytes]]
    ) -> str:
        return json.dumps(self.vision, ensure_ascii=False)


def make_rule(
    intent: str = "pytest 离线执行",
    source_path: str = "lots.csv",
    steps: Optional[List[Dict[str, Any]]] = None,
    options: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """构造一条最小可用规则；用例只需覆写关心的部分。"""
    rule: Dict[str, Any] = {
        "version": "2.0",
        "intent": intent,
        "source": {"path": source_path, "format": "csv"},
        "options": options or {},
        "steps": steps or [],
        "output": {"formats": ["csv"], "report": True},
    }
    rule.update(extra)
    return rule


__all__ = ["FixtureProvider", "make_rule"]

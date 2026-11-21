# -*- coding: utf-8 -*-
"""新测试的共享基建（仅提供工具函数/fixture，不改动任何现有测试）。

本文件位于 tests/ 目录，pytest 会自动加载。这里集中放置「描述 → /plan 生成
规则 → 语义自检」链路测试里反复出现的两个动作：

1. 从 /plan 返回的 HTML 中抽出 textarea[name=rule_json] 并解析成规则字典；
2. 用 monkeypatch 注入测试专用 Provider（FixtureProvider / ScriptedProvider），
   由 pytest 在用例结束时自动恢复，避免污染其他用例。

辅助函数保持「薄」：不做断言、不做重试，方便被多个测试文件组合使用。
"""
from __future__ import annotations

import html
import json
import re
from typing import Any, Dict

import rules

# 与 verify_scenario_matrix.py:22 一致：/plan 页面把规则写进该 textarea。
_RULE_AREA = re.compile(
    r'<textarea class="code" name="rule_json" rows="16"[^>]*>(.*?)</textarea>',
    re.S,
)


def extract_rule_from_plan_body(body: str) -> Dict[str, Any]:
    """从 webapp ``POST /plan`` 的 HTML 响应体中提取并解析规则 JSON。

    页面把生成结果写入 ``<textarea name="rule_json">``，内容经 HTML 转义，
    因此需要先 ``html.unescape`` 再 ``json.loads``。

    Args:
        body: ``webapp.handle_request("POST", "/plan", ...)`` 返回的 ``Reply.body``。

    Returns:
        解析后的规则字典（version 2.0 契约）。

    Raises:
        AssertionError: 页面未包含规则 textarea，或内容不是合法 JSON。
    """
    match = _RULE_AREA.search(body or "")
    if not match:
        raise AssertionError("/plan 页面未返回规则 JSON textarea")
    raw = html.unescape(match.group(1))
    try:
        rule = json.loads(raw)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性分支
        raise AssertionError(f"/plan 规则 JSON 解析失败：{exc}") from exc
    if not isinstance(rule, dict):
        raise AssertionError("/plan 规则 JSON 必须是对象")
    return rule


def patch_provider(monkeypatch: Any, provider: Any) -> Any:
    """把 ``rules.get_provider`` 替换为给定 Provider，并在用例结束时恢复。

    使用 pytest 的 ``monkeypatch`` fixture，因此会在测试 teardown 时自动还原，
    不影响同一进程里的其他用例（现有 791 passed 不会被污染）。

    ``provider`` 只需实现 ``llm_provider.LLMProvider`` 协议：``complete``
    方法；可选 ``complete_vision`` / ``name`` / ``mode`` / ``model`` /
    ``reason`` 属性。``tests.provider_fixture.FixtureProvider`` 与
    ``tests.industry_harness.ScriptedProvider`` 均可直接传入。

    Args:
        monkeypatch: pytest 内置的 ``monkeypatch`` fixture。
        provider: 测试注入的 Provider 实例。

    Returns:
        原样返回 ``provider``，便于调用方继续断言其调用记录（如 ``provider.calls``）。
    """
    monkeypatch.setattr(rules, "get_provider", lambda: provider)
    return provider


__all__ = ["extract_rule_from_plan_body", "patch_provider"]

# -*- coding: utf-8 -*-
"""从注册表生成给 LLM 的算子目录与契约说明。

算子目录不手写：新增算子只要注册，就会自动出现在提示词里，
保证"LLM 能用的算子"与"程序能执行的算子"永远一致。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ops.registry import all_ops, get_op, op_names


def _param_summary(spec) -> str:
    parts = []
    for p in spec.params:
        mark = "必填" if p.required else "可选"
        enum = f" ∈ {list(p.enum)}" if p.enum else ""
        parts.append(f"{p.name}({mark}{enum})")
    return "，".join(parts) if parts else "无必填参数"


def build_catalog(only: Optional[List[str]] = None) -> str:
    """渲染算子目录：每行 ``op: 说明 | 参数 | 示例``。

    ``only`` 给定时只渲染本场景需要的算子（按需引用），避免把 50+ 个无关
    算子塞进提示词稀释注意力；不给定时保持全量，兼容既有调用与测试。
    """
    if only is None:
        specs = all_ops()
    else:
        requested = list(dict.fromkeys(str(name) for name in only))
        specs = [spec for name in requested if (spec := get_op(name)) is not None]
    lines: List[str] = []
    for spec in specs:
        example = json.dumps(spec.prompt_example, ensure_ascii=False) if spec.prompt_example else ""
        line = f"- {spec.name}: {spec.summary or ''}"
        params = _param_summary(spec)
        if params != "无必填参数":
            line += f"｜参数：{params}"
        if example:
            line += f"｜示例：{example}"
        lines.append(line)
    return "\n".join(lines)


def build_examples() -> List[str]:
    """所有算子的契约示例（JSON 字符串）。"""
    out = []
    for spec in all_ops():
        if spec.prompt_example:
            out.append(json.dumps(spec.prompt_example, ensure_ascii=False))
    return out


def catalog_dict() -> Dict[str, Any]:
    return {"ops": op_names()}


__all__ = ["build_catalog", "build_examples", "catalog_dict"]

# -*- coding: utf-8 -*-
"""中间契约：由 OpSpec 派生的通用参数校验与 JSON Schema 导出。

校验只依赖算子声明（ParamSpec + 自定义 validate 钩子），新增算子无需
改动校验分派；同一份声明还能导出可供 LLM/文档使用的 JSON Schema。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ops.constants import as_list, aggregate_pairs, pct_alias
from ops.registry import OpSpec, get_op, op_names

_PY_TO_JSON = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _type_name(expected: Any) -> str:
    if isinstance(expected, tuple):
        return "|".join(_PY_TO_JSON.get(t, getattr(t, "__name__", str(t))) for t in expected)
    return _PY_TO_JSON.get(expected, getattr(expected, "__name__", str(expected)))


def validate_step(
    step: Dict[str, Any], known: set, tag: str, errors: List[str]
) -> None:
    """按 OpSpec 通用校验一个步骤（字段/类型/枚举/列引用 + 自定义钩子）。"""
    spec = get_op(str(step.get("op")))
    if spec is None:
        errors.append(f"{tag}.op 非法算子 {step.get('op')!r}，可选 {op_names()}")
        return
    for p in spec.params:
        if p.name not in step:
            if p.required:
                errors.append(f"{tag} 缺少字段 {p.name}")
            continue
        value = step[p.name]
        if p.type is not None and not isinstance(value, p.type):
            errors.append(f"{tag}.{p.name} 类型错误（期望 {_type_name(p.type)}）")
            continue
        if p.enum and value not in p.enum:
            errors.append(f"{tag}.{p.name} 非法，可选 {list(p.enum)}")
        if (p.column or p.multi) and known:
            for col in as_list(value):
                if not isinstance(col, str) or not col:
                    errors.append(f"{tag}.{p.name} 必须是列名")
                elif col not in known:
                    errors.append(f"{tag}.{p.name} 引用了不存在的列 {col!r}")
    if spec.validate is not None:
        spec.validate(step, known, tag, errors)


def step_outputs(step: Dict[str, Any]) -> List[str]:
    """步骤新增的列名（供链式引用校验）。"""
    spec = get_op(str(step.get("op")))
    if spec is None or spec.outputs is None:
        return []
    return [c for c in spec.outputs(step) if c]


def step_refs(step: Dict[str, Any]) -> Optional[List[Any]]:
    """步骤引用的输入列；None 表示该算子无需做列存在性校验。"""
    spec = get_op(str(step.get("op")))
    if spec is None:
        return None
    if spec.name == "union":
        return None
    return spec.refs(step) if spec.refs is not None else _generic_refs(step)


def _generic_refs(step: Dict[str, Any]) -> List[Any]:
    """通用输入列：与旧实现保持一致的键集合（subset 等不在此列）。"""
    refs: List[Any] = []
    for key in ("column", "columns", "by", "left", "within", "index"):
        refs.extend(as_list(step.get(key)))
    return refs


def to_json_schema(op_name: str) -> Optional[Dict[str, Any]]:
    """导出某算子的 JSON Schema 片段（供文档/外部工具消费）。"""
    spec = get_op(op_name)
    if spec is None:
        return None
    props: Dict[str, Any] = {"op": {"const": op_name, "type": "string"}}
    required = ["op"]
    for p in spec.params:
        prop: Dict[str, Any] = {}
        if p.type is not None:
            base = p.type[0] if isinstance(p.type, tuple) else p.type
            prop["type"] = _PY_TO_JSON.get(base, "string")
        if p.enum:
            prop["enum"] = list(p.enum)
        props[p.name] = prop
        if p.required:
            required.append(p.name)
    return {
        "title": spec.name,
        "description": spec.summary,
        "type": "object",
        "properties": props,
        "required": required,
    }


def catalog_schema() -> Dict[str, Any]:
    """导出全部算子的 schema 集合。"""
    return {name: to_json_schema(name) for name in op_names()}


def spec_contract(spec: OpSpec) -> Dict[str, Any]:
    """算子声明的紧凑摘要（参数名/必填/枚举）。"""
    return {
        "name": spec.name,
        "layer": spec.layer,
        "summary": spec.summary,
        "params": [
            {
                "name": p.name,
                "required": p.required,
                "enum": list(p.enum) if p.enum else None,
                "column": p.column or p.multi,
            }
            for p in spec.params
        ],
    }


__all__ = [
    "validate_step",
    "step_outputs",
    "step_refs",
    "to_json_schema",
    "catalog_schema",
    "spec_contract",
    "aggregate_pairs",
    "pct_alias",
]

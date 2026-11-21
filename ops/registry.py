# -*- coding: utf-8 -*-
"""算子注册表：声明式算子单元（OpSpec）与动态注册机制。

一个算子只在这里**声明一次**：参数、引用列、新增列、清洗保护列、校验钩子、
执行函数、角色、任务类型提示、提示词片段。核心分发、校验、提示词、
任务画像全部从注册表读取，新增算子无需改动多处 if-elif。

``role`` 是任务画像判定语义的单一事实源：
- filter   ：只筛行，不改变分析口径（filter / dedupe / head ...）
- transform：列与值的变换，可能产生时间分量列但不构成结果维度
- analysis ：决定最终结果形态的算子（aggregate / pct / pivot / corr ...）
- output   ：校验/审计类，不改变业务数据
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ParamSpec:
    """单个参数的契约声明。"""

    name: str
    type: Any = None            # python 类型或类型元组；None 表示不限制
    required: bool = False
    default: Any = None
    enum: Tuple[Any, ...] = ()
    column: bool = False        # 该参数是"单个列名引用"
    multi: bool = False         # 该参数是"列名列表引用"


@dataclass
class OpSpec:
    """一个算子的完整声明（单一事实源）。"""

    name: str
    layer: int                                  # 1=单算子 2=分析算子
    execute: Callable                           # (df, step, frames, ctx) -> (df, note)
    params: Tuple[ParamSpec, ...] = ()
    outputs: Optional[Callable[[Dict[str, Any]], List[str]]] = None   # 新增列
    refs: Optional[Callable[[Dict[str, Any]], List[str]]] = None      # 输入列（列存在性校验）
    validate: Optional[Callable[[Dict[str, Any], set, str, List[str]], None]] = None
    preserve_refs: Optional[Callable[[Dict[str, Any]], List[str]]] = None  # 清洗后需还原空值的列
    task_hint: str = ""                         # proportion/ranking/trend/distribution/pivot/join/summary
    role: str = "transform"                     # filter/transform/analysis/output（见模块说明）
    produces_time_dim: bool = False             # 是否产生时间维度列（供趋势判定）
    summary: str = ""                           # 一句话说明（提示词/文档）
    prompt_example: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)


_REGISTRY: Dict[str, OpSpec] = {}
_LOADED = False


def register_op(spec: OpSpec) -> OpSpec:
    """注册一个算子；同名重复注册视为编程错误。"""
    if spec.name in _REGISTRY:
        raise ValueError(f"算子重复注册：{spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def ensure_loaded() -> None:
    """惰性导入内置算子，触发装饰式注册。

    先导入再置位：若导入中途抛错，标志保持 False，下次调用会重试，
    避免"标志已置位但算子未注册"的隐性故障。
    """
    global _LOADED
    if _LOADED:
        return
    import ops.builtin  # noqa: F401  导入即注册
    _LOADED = True


def get_op(name: str) -> Optional[OpSpec]:
    ensure_loaded()
    return _REGISTRY.get(name)


def all_ops() -> List[OpSpec]:
    ensure_loaded()
    return sorted(_REGISTRY.values(), key=lambda s: (s.layer, s.name))


def op_names() -> List[str]:
    ensure_loaded()
    return sorted(_REGISTRY)


def ops_by_layer(layer: int) -> List[OpSpec]:
    return [s for s in all_ops() if s.layer == layer]


__all__ = [
    "ParamSpec",
    "OpSpec",
    "register_op",
    "ensure_loaded",
    "get_op",
    "all_ops",
    "op_names",
    "ops_by_layer",
]

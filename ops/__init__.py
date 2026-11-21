# -*- coding: utf-8 -*-
"""可生长、可组合的算子体系。

- 分层原语：``primitives``（L0）/ ``builtin``（L1 单算子、L2 分析算子）
- 动态扩展：``registry``（OpSpec + @register_op + 自动发现）
- 精确契约：``contract``（ParamSpec 校验 + JSON Schema 导出）
- 编译降解：``compiler``（宽松规则 → ExecutablePlan）
- 提示派生：``prompt``（从注册表生成 LLM 算子目录）
"""
from __future__ import annotations

from ops.compiler import ExecutablePlan, compile_rule, reconcile_rule_columns, validate_steps
from ops.contract import (
    catalog_schema, spec_contract, step_outputs, step_refs, to_json_schema, validate_step,
)
from ops.errors import RuleValidationError
from ops.registry import (
    OpSpec, ParamSpec, all_ops, ensure_loaded, get_op, op_names, ops_by_layer, register_op,
)

__all__ = [
    "RuleValidationError",
    "OpSpec",
    "ParamSpec",
    "register_op",
    "ensure_loaded",
    "get_op",
    "all_ops",
    "op_names",
    "ops_by_layer",
    "ExecutablePlan",
    "compile_rule",
    "reconcile_rule_columns",
    "validate_steps",
    "validate_step",
    "step_outputs",
    "step_refs",
    "to_json_schema",
    "catalog_schema",
    "spec_contract",
]

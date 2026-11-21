"""注册表与契约测试：动态注册、自动发现、Schema 导出、即插即用。"""
from __future__ import annotations

import pandas as pd
import pytest

import rule_executor as re
from ops import compiler
from ops.contract import catalog_schema, to_json_schema
from ops.registry import OpSpec, ParamSpec, get_op, op_names, register_op

EXPECTED_OPS = {
    "select", "drop_columns", "rename", "filter", "sort", "derive", "head",
    "aggregate", "pct", "pivot", "dedupe", "cast", "fill", "split", "concat",
    "string", "date_part", "bin", "assign", "rank", "sample", "join", "union",
}


def test_all_builtin_ops_registered():
    names = set(op_names())
    assert EXPECTED_OPS <= names
    for name in EXPECTED_OPS:
        assert get_op(name) is not None


def test_contract_schema_export():
    schema = to_json_schema("pct")
    assert schema["properties"]["op"]["const"] == "pct"
    assert "scope" in schema["properties"]
    full = catalog_schema()
    assert EXPECTED_OPS <= set(full)


def test_op_layers():
    assert get_op("filter").layer == 1
    assert get_op("pct").layer == 2
    assert get_op("join").layer == 2


def test_plugin_op_is_usable_end_to_end():
    """新增算子只需注册即可被校验、编译、执行 —— 无需改动核心分发。"""

    def _echo(df, step, frames=None, ctx=None):
        out = df.copy()
        out["echo"] = step.get("value")
        return out, "echo"

    spec = OpSpec(
        name="demo_echo", layer=1, execute=_echo,
        params=(ParamSpec("value", str, required=True),),
        outputs=lambda s: ["echo"], summary="测试用回显算子",
    )
    register_op(spec)
    assert get_op("demo_echo") is spec
    assert "demo_echo" in op_names()

    # 校验/编译
    plan = compiler.compile_rule(
        {"version": "2.0", "steps": [{"op": "demo_echo", "value": "x"}]}, ["a"]
    )
    assert plan.steps[0]["op"] == "demo_echo"
    assert "echo" in plan.columns_out

    # 执行
    out, _note = re.apply_step(pd.DataFrame({"a": [1, 2]}), {"op": "demo_echo", "value": "x"})
    assert "echo" in out.columns and out["echo"].iloc[0] == "x"


def test_plugin_missing_required_field_rejected():
    with pytest.raises(Exception):
        compiler.compile_rule(
            {"version": "2.0", "steps": [{"op": "demo_echo"}]}, ["a"]
        )

# -*- coding: utf-8 -*-
"""全算子矩阵测试：注册 / 契约校验 / Schema / 执行 / 输出列 / 空数据鲁棒性。

覆盖注册表中全部 53 个算子。每个算子给出一份可执行的最小 step，统一验证：
1. 算子已注册且有 role；
2. validate_steps 通过（参数/列引用合法）；
3. compile_rule 可编译；
4. apply_step 真实执行不抛异常，返回 DataFrame；
5. 声明的 outputs 列出现在结果中（契约一致）；
6. JSON Schema 可导出；
7. 空数据下不出现未捕获的 pandas 异常（只允许 RuleValidationError）。
"""
from __future__ import annotations

import pandas as pd
import pytest

import rule_executor as re
from ops.compiler import compile_rule, validate_steps
from ops.contract import to_json_schema
from ops.errors import RuleValidationError
from ops.registry import all_ops, get_op

# ------------------------------------------------------------
# 测试数据：覆盖所有算子所需的列类型
# ------------------------------------------------------------
COLUMNS = [
    "id", "分组", "数值", "数值2", "日期", "开始时间", "结束时间",
    "文本", "名称", "类别", "金额", "面积", "手机", "状态", "键", "组合源",
]


def _frame(rows: int = 8) -> pd.DataFrame:
    n = rows
    return pd.DataFrame({
        "id": list(range(1, n + 1)),
        "分组": (["A", "B"] * ((n + 1) // 2))[:n],
        "数值": ([1.0, None, 3.0, 10.0, 5.0, 2.0, 8.0, 4.0] * ((n // 8) + 1))[:n],
        "数值2": ([2.0, 4.0, 6.0, 1.0, 3.0, 9.0, 7.0, 5.0] * ((n // 8) + 1))[:n],
        "日期": (["2026-01-05", "2026-02-10", "2025-12-01", "2026-03-15",
                "2026/04/20", "2026-05-06", "2025-11-11", "2026-06-30"] * ((n // 8) + 1))[:n],
        "开始时间": (["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01",
                   "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"] * ((n // 8) + 1))[:n],
        "结束时间": (["2026-01-11", "2026-02-12", "2026-03-13", "2026-04-14",
                   "2026-05-15", "2026-06-16", "2026-07-17", "2026-08-18"] * ((n // 8) + 1))[:n],
        "文本": (["  AbC  ", "ＸＹＺ", "a\u200bb", "hello", "  ", "World", "MiXeD", "123"] * ((n // 8) + 1))[:n],
        "名称": (["北京华置业有限公司", "北京华置业公司", "上海某某集团", "广州测试厂",
                "北京华置业有限公司", "上海某某集团", "深圳示例店", "杭州演示公司"] * ((n // 8) + 1))[:n],
        "类别": (["北京市", "北京市", "上海", "罕见X", "北京市", "上海", "罕见Y", "北京"] * ((n // 8) + 1))[:n],
        "金额": (["1.2万", "¥1,234", "100元", "3.5万元", "¥2,000", "50", "1亿", "¥9,999"] * ((n // 8) + 1))[:n],
        "面积": (["120㎡", "80平方米", "1.5万㎡", "60", "90平米", "100", "200㎡", "75"] * ((n // 8) + 1))[:n],
        "手机": (["13800138000", "13912345678", "13600001111", "15022223333",
                "13800138000", "13777778888", "13555556666", "13333334444"] * ((n // 8) + 1))[:n],
        "状态": (["是", "否", "是", "是", "否", "是", "否", "是"] * ((n // 8) + 1))[:n],
        "键": (["k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8"] * ((n // 8) + 1))[:n],
        "组合源": (["pre-mid-post", "a-b-c", "x-y-z", "1-2-3", "p-q-r",
                  "u-v-w", "m-n-o", "e-f-g"] * ((n // 8) + 1))[:n],
    })


RIGHT = pd.DataFrame({"键": ["k1", "k2", "k3", "k4"], "右值": [100, 200, 300, 400],
                      "分组": ["A", "B", "A", "B"], "数值": [1.0, 2.0, 3.0, 4.0]})
FRAMES = {"right": RIGHT}

# 每个算子一份可执行 step（键 = 算子名）
STEPS = {
    # ---- layer 1 ----
    "assert": {"op": "assert", "checks": [{"column": "数值", "operator": "gte", "value": 0}], "action": "flag"},
    "assign": {"op": "assign", "column": "等级", "rules": [
        {"condition": {"column": "数值", "operator": "gt", "value": 5}, "value": "高"}], "default": "低"},
    "bin": {"op": "bin", "column": "数值", "bins": 3, "method": "width"},
    "cast": {"op": "cast", "columns": ["id"], "to": "float"},
    "category_map": {"op": "category_map", "column": "类别", "mapping": {"北京市": "北京"}, "min_count": 2},
    "clip": {"op": "clip", "column": "数值", "lower": 1, "upper": 5},
    "concat": {"op": "concat", "columns": ["分组", "状态"], "sep": "-", "into": "组合"},
    "convert_unit": {"op": "convert_unit", "columns": ["金额"], "kind": "money"},
    "cross_column_filter": {"op": "cross_column_filter", "left": "数值", "right": "数值2", "operator": "gt"},
    "cumsum": {"op": "cumsum", "column": "数值", "into": "累计"},
    "date_diff": {"op": "date_diff", "left": "结束时间", "right": "开始时间", "unit": "day"},
    "date_filter": {"op": "date_filter", "column": "日期", "year": 2026},
    "date_parse": {"op": "date_parse", "columns": ["日期"], "formats": ["%Y-%m-%d", "%Y/%m/%d"]},
    "date_part": {"op": "date_part", "column": "日期", "parts": ["year", "month"]},
    "dedupe": {"op": "dedupe", "subset": ["分组"], "keep": "first"},
    "dedupe_fuzzy": {"op": "dedupe_fuzzy", "columns": ["名称"], "similarity": 0.8},
    "derive": {"op": "derive", "column": "比值", "operator": "ratio", "left": "数值", "right": "数值2"},
    "expr": {"op": "expr", "column": "公式列", "expression": "c0 / c1", "operands": ["数值", "数值2"]},
    "drop_columns": {"op": "drop_columns", "columns": ["数值2"]},
    "dropna": {"op": "dropna", "subset": ["数值"], "how": "any"},
    "fill": {"op": "fill", "columns": ["数值"], "method": "median"},
    "filter": {"op": "filter", "column": "分组", "operator": "eq", "value": "A"},
    "having": {"op": "having", "column": "数值", "operator": "gte", "value": 2},
    "head": {"op": "head", "n": 3},
    "lag": {"op": "lag", "columns": ["数值"], "periods": 1},
    "mask_pii": {"op": "mask_pii", "columns": ["手机"], "kinds": ["phone"], "mode": "partial"},
    "normalize": {"op": "normalize", "column": "数值", "method": "minmax"},
    "outlier": {"op": "outlier", "column": "数值", "method": "iqr", "action": "flag"},
    "rank": {"op": "rank", "by": "数值", "into": "排名"},
    "rename": {"op": "rename", "mapping": {"分组": "组"}},
    "replace": {"op": "replace", "columns": ["状态"], "mapping": {"是": "Y", "否": "N"}},
    "rolling": {"op": "rolling", "column": "数值", "window": 2, "func": "mean"},
    "sample": {"op": "sample", "n": 3, "random_state": 42},
    "select": {"op": "select", "columns": ["分组", "数值"]},
    "sort": {"op": "sort", "by": "数值", "ascending": False},
    "split": {"op": "split", "column": "组合源", "sep": "-", "into": ["段1", "段2"]},
    "string": {"op": "string", "column": "文本", "action": "strip"},
    "tail": {"op": "tail", "n": 3},
    "text_normalize": {"op": "text_normalize", "columns": ["文本"]},
    # ---- layer 2 ----
    "aggregate": {"op": "aggregate", "by": ["分组"], "column": "数值", "func": "sum", "alias": "合计"},
    "anti_join": {"op": "anti_join", "right": "right", "left_on": "键", "right_on": "键"},
    "corr": {"op": "corr", "columns": ["数值", "数值2"]},
    "count_rows": {"op": "count_rows", "by": ["分组"], "alias": "条数"},
    "describe": {"op": "describe", "columns": ["数值", "数值2"]},
    "group_rank": {"op": "group_rank", "by": "分组", "column": "数值", "topn": 2},
    "join": {"op": "join", "right": "right", "how": "left", "left_on": "键", "right_on": "键"},
    "melt": {"op": "melt", "id_vars": ["分组"], "value_vars": ["数值", "数值2"]},
    "pct": {"op": "pct", "scope": "condition", "condition": {"column": "分组", "operator": "eq", "value": "A"}, "alias": "占比"},
    "pivot": {"op": "pivot", "index": "分组", "columns": "状态", "values": "数值", "aggfunc": "sum"},
    "profile": {"op": "profile"},
    "rate": {"op": "rate", "column": "数值", "condition": {"column": "数值", "operator": "notna"},
             "true_label": "有值", "false_label": "空值", "alias": "有值率"},
    "resample": {"op": "resample", "column": "日期", "freq": "M", "funcs": ["mean"]},
    "semi_join": {"op": "semi_join", "right": "right", "left_on": "键", "right_on": "键"},
    "union": {"op": "union", "right": "right"},
}

ALL_NAMES = [s.name for s in all_ops()]


def test_step_matrix_covers_every_operator():
    assert set(ALL_NAMES) == set(STEPS), (
        f"未覆盖: {sorted(set(ALL_NAMES) - set(STEPS))}; "
        f"多余: {sorted(set(STEPS) - set(ALL_NAMES))}"
    )


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_registered_with_role(name):
    spec = get_op(name)
    assert spec is not None
    assert spec.role in ("filter", "transform", "analysis", "output")
    assert callable(spec.execute)


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_validate_steps(name):
    step = STEPS[name]
    errors = validate_steps([step], COLUMNS)
    assert errors == [], f"{name}: {errors}"


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_compile(name):
    plan = compile_rule({"version": "2.0", "steps": [STEPS[name]]}, COLUMNS)
    assert len(plan.steps) == 1
    assert plan.steps[0]["op"] == name


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_execute_returns_dataframe(name):
    df = _frame()
    out, note = re.apply_step(df, dict(STEPS[name]), FRAMES, {})
    assert isinstance(out, pd.DataFrame), name
    assert isinstance(note, str) and note, name


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_declared_outputs_present(name):
    spec = get_op(name)
    if spec.outputs is None:
        pytest.skip(f"{name} 未声明 outputs")
    declared = [c for c in spec.outputs(STEPS[name]) if c]
    if not declared:
        pytest.skip(f"{name} 当前 step 无声明输出列")
    out, _ = re.apply_step(_frame(), dict(STEPS[name]), FRAMES, {})
    missing = [c for c in declared if c not in out.columns]
    assert not missing, f"{name} 声明输出 {declared}，结果缺少 {missing}；实际列 {list(out.columns)}"


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_schema_export(name):
    schema = to_json_schema(name)
    assert schema is not None
    assert schema["properties"]["op"]["const"] == name


@pytest.mark.parametrize("name", ALL_NAMES)
def test_op_empty_data_no_raw_exception(name):
    """空数据下允许 RuleValidationError（明确的业务错误），不允许 pandas 原始异常。"""
    empty = _frame(0)
    try:
        out, _ = re.apply_step(empty, dict(STEPS[name]), FRAMES, {})
        assert isinstance(out, pd.DataFrame), name
    except RuleValidationError:
        pass
    except (KeyError, AttributeError, TypeError, ValueError, ZeroDivisionError, IndexError) as exc:
        pytest.fail(f"{name} 在空数据下抛出未捕获的 {type(exc).__name__}: {exc}")

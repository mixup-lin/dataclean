"""算子回归：行/值/数值/时间/重塑 + P0 画像修复 + P1 新增算子。

覆盖用户真实诉求：「统计市是福州，结束时间在2026年，小区不为空的数据，
按小区分组，统计每个小区有几条数据」——回归两处根因：
1. 旧关键词 Mock 会漏掉「市」条件、并把日期筛成 date_part 中间列；
2. 画像层把 date_part 当趋势依据，把分组计数误判成趋势分析。

Mock 模块已移除，全部用例改用显式规则 JSON（tests.provider_fixture）。
"""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest

import rule_executor as re
import rules
from ops.contract import catalog_schema
from ops.prompt import build_catalog
from ops.registry import all_ops, get_op, op_names
from task_profile import detect_task_type
from tests.provider_fixture import FixtureProvider, make_rule

NEW_OPS = (
    "tail", "dropna", "replace", "clip", "normalize",
    "outlier", "date_diff", "cumsum", "melt", "describe",
)
P0_OPS = ("date_filter", "count_rows", "cross_column_filter")
P1_OPS = (
    "convert_unit", "text_normalize", "category_map", "dedupe_fuzzy",
    "assert", "profile", "mask_pii", "date_parse", "resample",
    "rolling", "lag", "corr", "having", "group_rank",
    "anti_join", "semi_join",
)

USER_INTENT = "统计市是福州，结束时间在2026年，小区不为空的数据，按小区分组，统计每个小区有几条数据"
USER_COLUMNS = ["市", "省", "小区", "结束时间", "成交价(元)", "所在楼层", "总楼层"]

SAMPLE_CSV = """市,小区,结束时间,成交价(元)
福州,A,2026-03-01,100
福州,,2026-04-01,200
福州,B,2025-12-01,300
厦门,C,2026-05-01,400
福州,B,2026-06-01,500
"""

EN_CSV = """city,end_time,community,price
福州,2026-03-01,Alpha,100
福州,2026-04-01,,200
福州,2025-12-01,Beta,300
厦门,2026-05-01,Gamma,400
福州,2026-06-01,Beta,500
"""


def _write_csv(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8-sig")
    return str(path)


def _fuzhou_rule(source: str) -> dict:
    """用户诉求的**显式规则**：一步日期筛选 + 一步分组计数。"""
    return make_rule(
        intent=USER_INTENT, source_path=source,
        options={"fill_missing": "median", "outlier_method": "none"},
        # 故意不写 task_type：验证「推理」本身不再把分组计数判成趋势
        steps=[
            {"op": "filter", "conditions": [
                {"column": "市", "operator": "eq", "value": "福州"},
                {"column": "小区", "operator": "notna"},
            ], "logic": "and"},
            {"op": "date_filter", "column": "结束时间", "year": 2026},
            {"op": "count_rows", "by": "小区", "alias": "条数"},
        ],
        output={"formats": ["csv"], "report": True},
    )


# ============================================================
# 注册表 / 白名单一致性
# ============================================================
def test_new_ops_registered_and_in_catalog():
    names = set(op_names())
    assert set(NEW_OPS) <= names
    catalog = build_catalog()
    for name in NEW_OPS:
        assert f"- {name}:" in catalog
        assert catalog_schema()[name]["title"] == name


def test_new_ops_in_rule_whitelist():
    assert set(NEW_OPS) <= set(rules.STEP_OPS)


def test_rule_whitelist_matches_registry():
    """规则白名单动态派生自注册表，避免「能执行但不能生成 / 能生成但被剔除」。"""
    assert set(rules.STEP_OPS) == set(op_names())


@pytest.mark.parametrize("name", NEW_OPS + P0_OPS + P1_OPS)
def test_each_op_has_contract(name):
    spec = get_op(name)
    assert spec is not None and spec.summary
    assert spec.prompt_example and spec.prompt_example.get("op") == name
    assert spec.role in ("filter", "transform", "analysis", "output")


def test_all_ops_have_role_and_metadata():
    from ops.constants import OP_ROLES

    specs = all_ops()
    assert len(specs) >= 50
    for spec in specs:
        assert spec.role in OP_ROLES, spec.name
        assert not hasattr(spec, "mock_keywords"), spec.name
        assert spec.summary, spec.name


# ============================================================
# 行操作
# ============================================================
@pytest.mark.parametrize("name", ["tail", "dropna", "replace", "clip", "normalize",
                                  "outlier", "date_diff", "cumsum", "melt", "describe"])
def test_apply_step_dispatches_new_op(name):
    get_op(name)
    df = pd.DataFrame({"a": [1, 2, None], "b": [3, 4, 5], "t": ["2026-01-01"] * 3})
    steps = {
        "tail": {"op": "tail", "n": 1},
        "dropna": {"op": "dropna", "subset": ["a"]},
        "replace": {"op": "replace", "columns": ["b"], "mapping": {3: 30}},
        "clip": {"op": "clip", "column": "b", "lower": 4},
        "normalize": {"op": "normalize", "column": "b", "method": "minmax"},
        "outlier": {"op": "outlier", "column": "b", "action": "flag"},
        "date_diff": {"op": "date_diff", "left": "t", "right": "t", "unit": "day"},
        "cumsum": {"op": "cumsum", "column": "b"},
        "melt": {"op": "melt", "id_vars": ["t"], "value_vars": ["a", "b"]},
        "describe": {"op": "describe", "columns": ["b"]},
    }
    out, note = re.apply_step(df, steps[name])
    assert isinstance(out, pd.DataFrame) and note


def test_tail_keeps_last_rows():
    df = pd.DataFrame({"a": [1, 2, 3]})
    out, note = re.apply_step(df, {"op": "tail", "n": 2})
    assert list(out["a"]) == [2, 3] and "取后 2 行" in note


def test_dropna_subset_and_how():
    df = pd.DataFrame({"a": [1, None, None], "b": [1, None, 3]})
    out_any, _ = re.apply_step(df, {"op": "dropna", "subset": ["a", "b"], "how": "any"})
    assert len(out_any) == 1
    out_all, _ = re.apply_step(df, {"op": "dropna", "subset": ["a", "b"], "how": "all"})
    assert len(out_all) == 2


def test_dropna_thresh():
    df = pd.DataFrame({"a": [1, None], "b": [None, None], "c": [1, 2]})
    out, _ = re.apply_step(df, {"op": "dropna", "thresh": 2})
    assert len(out) == 1


def test_dropna_preserves_original_nulls():
    assert re._filter_columns({"steps": [{"op": "dropna", "subset": ["小区"]}]}) == {"小区"}
    assert re._filter_columns({"steps": [{"op": "dropna"}]}, ["a", "b"]) == {"a", "b"}


def test_explicit_delete_missing_uses_dropna_step():
    """不再靠关键词模板猜：删除缺失用显式 dropna 步骤，且不动 fill_missing。"""
    rule = make_rule(steps=[{"op": "dropna", "subset": ["小区"]}])
    assert rule["options"].get("fill_missing") != "drop"
    assert rule["steps"][0]["op"] == "dropna"
    rules.assert_valid_rule(rule, ["小区"])


def test_bin_then_aggregate_is_distribution():
    rule = {"steps": [
        {"op": "bin", "column": "成交价(元)", "bins": 5},
        {"op": "count_rows", "by": "成交价(元)_分箱", "alias": "条数"},
    ]}
    assert detect_task_type(rule) == "distribution"


# ============================================================
# 值 / 数值
# ============================================================
def test_replace_mapping_and_sugar():
    df = pd.DataFrame({"状态": ["成交", "流拍"], "备注": ["成交", "无"]})
    out, note = re.apply_step(
        df, {"op": "replace", "columns": ["状态"], "mapping": {"成交": "已成交"}}
    )
    assert list(out["状态"]) == ["已成交", "流拍"]
    assert list(out["备注"]) == ["成交", "无"] and "值替换" in note


def test_clip_bounds():
    df = pd.DataFrame({"v": [-5, 0, 50, 500]})
    out, _ = re.apply_step(df, {"op": "clip", "column": "v", "lower": 0, "upper": 100})
    assert list(out["v"]) == [0, 0, 50, 100]


def test_normalize_methods():
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0]})
    mini, _ = re.apply_step(df, {"op": "normalize", "column": "v", "method": "minmax"})
    assert list(mini["v_minmax"]) == [0.0, 0.5, 1.0]
    r, _ = re.apply_step(df, {"op": "normalize", "column": "v", "method": "rank_pct"})
    assert list(r["v_rank_pct"]) == [pytest.approx(1 / 3), pytest.approx(2 / 3), pytest.approx(1.0)]


def test_outlier_flag_clip_remove():
    df = pd.DataFrame({"v": [1, 2, 3, 4, 100]})
    flag, _ = re.apply_step(df, {"op": "outlier", "column": "v", "action": "flag"})
    assert bool(flag["v_异常"].iloc[-1]) and int(flag["v_异常"].sum()) == 1
    clipped, _ = re.apply_step(df, {"op": "outlier", "column": "v", "action": "clip"})
    assert clipped["v"].max() < 100
    removed, _ = re.apply_step(df, {"op": "outlier", "column": "v", "action": "remove"})
    assert len(removed) == 4


def test_cumsum_overall_and_grouped():
    df = pd.DataFrame({"g": ["a", "a", "b"], "v": [1, 2, 10]})
    out, _ = re.apply_step(df, {"op": "cumsum", "column": "v"})
    assert list(out["v_累计"]) == [1, 3, 13]
    grouped, _ = re.apply_step(df, {"op": "cumsum", "column": "v", "by": "g", "into": "c"})
    assert list(grouped["c"]) == [1, 3, 10]


def test_describe_stats():
    df = pd.DataFrame({"v": [1, 2, 3, None]})
    out, note = re.apply_step(df, {"op": "describe", "columns": ["v"]})
    row = out.iloc[0]
    assert row["列"] == "v" and int(row["非空数"]) == 3
    assert float(row["均值"]) == pytest.approx(2.0) and float(row["最大"]) == 3.0
    assert "统计概览" in note


# ============================================================
# 时间 / 重塑
# ============================================================
def test_date_diff_units():
    df = pd.DataFrame({
        "start": ["2026-01-01", "2026-01-01"],
        "end": ["2026-01-11", "2025-11-01"],
    })
    out, _ = re.apply_step(df, {"op": "date_diff", "left": "end", "right": "start", "unit": "day"})
    assert float(out["end_距start"].iloc[0]) == 10.0


def test_melt_wide_to_long():
    df = pd.DataFrame({"小区": ["A", "B"], "2025": [1, 2], "2026": [3, 4]})
    out, note = re.apply_step(
        df, {"op": "melt", "id_vars": ["小区"], "value_vars": ["2025", "2026"]}
    )
    assert list(out.columns) == ["小区", "变量", "值"] and len(out) == 4
    assert set(out["变量"]) == {"2025", "2026"} and "宽转长" in note


# ============================================================
# 校验：枚举/必填/列引用
# ============================================================
def test_new_op_validation_errors():
    known = ["a", "b"]
    cases = [
        {"op": "normalize", "column": "a", "method": "wrong"},
        {"op": "outlier", "column": "a", "method": "wrong"},
        {"op": "outlier", "column": "a", "action": "wrong"},
        {"op": "date_diff", "left": "a"},
        {"op": "melt"},
        {"op": "replace"},
        {"op": "clip", "column": "a"},
        {"op": "dropna", "how": "wrong"},
        {"op": "tail", "n": 0},
        {"op": "date_filter", "column": "a"},
        {"op": "resample", "column": "a"},
        {"op": "rolling", "column": "a", "window": 0},
    ]
    for step in cases:
        assert rules.validate_rule(
            {"version": "2.0", "intent": "x", "source": {"path": "x.csv", "format": "csv"},
             "steps": [step]}, known,
        ), step


def test_new_op_rejects_unknown_column():
    errs = rules.validate_rule(
        {"version": "2.0", "intent": "x", "source": {"path": "x.csv", "format": "csv"},
         "steps": [{"op": "clip", "column": "不存在", "upper": 1}]}, ["a"],
    )
    assert any("不存在" in e for e in errs)


def test_describe_detected_as_summary():
    assert detect_task_type({"steps": [{"op": "describe"}]}) == "summary"


# ============================================================
# P0：分组计数误判为趋势的根因回归
# ============================================================
def test_fuzhou_2026_group_count_end_to_end(tmp_path):
    src = _write_csv(tmp_path, "src.csv", SAMPLE_CSV)
    rule = _fuzhou_rule(src)
    summary = re.execute_rule(rule, base_output=str(tmp_path))

    assert summary["task"]["task_type"] == "summary"
    result = pd.read_csv(os.path.join(str(tmp_path), summary["run_id"], "result.csv"))
    assert set(result.columns) == {"小区", "条数"}
    assert result.to_dict("records") == [{"小区": "A", "条数": 1}, {"小区": "B", "条数": 1}]
    assert all(rec["status"] == "ok" for rec in summary["step_logs"])
    assert "趋势" not in summary["task"]["title"]


def test_explicit_task_type_overrides_inference_when_consistent():
    """显式声明与步骤形态一致时优先采信；明显误标则回退自动推断。"""
    consistent = {"task_type": "distribution", "steps": [
        {"op": "bin", "column": "成交价(元)", "bins": 5},
        {"op": "count_rows", "by": "成交价(元)_分箱", "alias": "条数"},
    ]}
    assert detect_task_type(consistent) == "distribution"

    # 历史 bug：LLM 把「按小区统计条数」写成 distribution，报告成了分布分析
    wrong = {"task_type": "distribution", "steps": [
        {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
        {"op": "count_rows", "by": "小区", "alias": "条数"},
    ]}
    assert detect_task_type(wrong) == "summary"


def test_skipped_date_part_does_not_trigger_trend():
    """聚合之后被 skipped 的 date_part 不能成为趋势依据（历史 bug）。"""
    frame = pd.DataFrame({"小区": ["A", "B"], "结束时间_年": [2026, 2026], "条数": [1, 1]})
    rule = {"steps": [
        {"op": "count_rows", "by": "小区", "alias": "条数"},
        {"op": "date_part", "column": "结束时间", "parts": ["year"]},
    ]}
    logs = [
        {"index": 0, "op": "count_rows", "status": "ok"},
        {"index": 1, "op": "date_part", "status": "skipped"},
    ]
    assert detect_task_type(rule, frame, logs) == "summary"


def test_trend_requires_at_least_two_periods():
    rule = {"steps": [
        {"op": "date_part", "column": "结束时间", "parts": ["year"]},
        {"op": "count_rows", "by": "结束时间_年", "alias": "条数"},
    ]}
    one_period = pd.DataFrame({"结束时间_年": [2026], "条数": [5]})
    two_periods = pd.DataFrame({"结束时间_年": [2025, 2026], "条数": [3, 5]})
    assert detect_task_type(rule, one_period) == "summary"
    assert detect_task_type(rule, two_periods) == "trend"


def test_compiler_alias_only_rewrites_condition_columns():
    from ops.compiler import _rewrite_alias

    step = {
        "op": "filter", "by": "年", "index": "年", "group_by": "年",
        "conditions": [{"column": "年", "operator": "eq", "value": 2026}],
    }
    _rewrite_alias(step, {"年": "结束时间_年"})
    assert step["conditions"][0]["column"] == "结束时间_年"
    assert step["by"] == "年" and step["index"] == "年" and step["group_by"] == "年"


def test_compile_keeps_aggregate_by_untouched_by_alias():
    from ops.compiler import compile_rule

    rule = {"version": "2.0", "steps": [
        {"op": "date_part", "column": "结束时间", "parts": ["year"]},
        {"op": "filter", "column": "年", "operator": "eq", "value": 2026},
        {"op": "count_rows", "by": "小区", "alias": "条数"},
    ]}
    plan = compile_rule(rule, ["结束时间", "小区"])
    assert plan.steps[1]["column"] == "结束时间_年"
    assert plan.steps[2]["by"] == "小区"


def test_parse_column_aliases_formats():
    mapping, warns = rules.parse_column_aliases(
        "地区 = city\n结束时间: end_time\n# 注释行\n小区\tcommunity\n\n",
        ["city", "end_time", "community"],
    )
    assert mapping == {"地区": "city", "结束时间": "end_time", "小区": "community"}
    assert warns == []
    assert rules.parse_column_aliases({"地区": "city"}, ["city"]) == ({"地区": "city"}, [])


def test_parse_column_aliases_rejects_unknown_column():
    mapping, warns = rules.parse_column_aliases("地区=city_name", ["city"])
    assert mapping == {}
    assert warns and "不存在" in warns[0]


def test_generate_rules_applies_user_mapping():
    """显式规则链路也会把使用者映射写进规则与提示词，不做任何内置语义猜测。"""
    english = ["city", "end_time", "community", "price"]
    rule = make_rule(source_path="x.csv", steps=[{"op": "filter", "column": "city", "operator": "eq", "value": "福州"}])
    generated, meta = rules.generate_rules(
        USER_INTENT, {"path": "x.csv", "format": "csv"}, english,
        provider=FixtureProvider(rule=rule),
        column_aliases={"地区": "city", "结束时间": "end_time", "小区": "community"},
    )
    assert generated["column_aliases"] == {
        "地区": "city", "结束时间": "end_time", "小区": "community",
    }
    assert meta["column_aliases"] == generated["column_aliases"]
    _system, user = rules.build_rule_prompt(
        USER_INTENT, {"path": "x.csv", "format": "csv"}, english,
        column_aliases={"地区": "city"},
    )
    assert json.loads(user)["column_aliases"] == {"地区": "city"}


def test_reconcile_accepts_english_date_part_alias():
    rule = {
        "version": "2.0", "intent": "x",
        "source": {"path": "x.csv", "format": "csv"},
        "steps": [
            {"op": "date_part", "column": "end_time", "parts": ["year"]},
            {"op": "filter", "column": "end_time_year", "operator": "eq", "value": 2026},
            {"op": "count_rows", "by": "community", "alias": "条数"},
        ],
        "options": {}, "output": {"formats": ["csv"], "report": False},
    }
    fixed, warns = rules.reconcile_rule_columns(rule, ["end_time", "community"])
    assert [s["op"] for s in fixed["steps"]] == ["date_part", "filter", "count_rows"]
    assert fixed["steps"][1]["column"] == "end_time_年"
    assert not [w for w in warns if "不存在的列" in w]


def test_english_columns_end_to_end(tmp_path):
    src = _write_csv(tmp_path, "en.csv", EN_CSV)
    rule = make_rule(
        source_path=src,
        options={"fill_missing": "median", "outlier_method": "none"},
        steps=[
            {"op": "filter", "column": "city", "operator": "eq", "value": "福州"},
            {"op": "filter", "column": "community", "operator": "notna"},
            {"op": "date_filter", "column": "end_time", "year": 2026},
            {"op": "count_rows", "by": "community", "alias": "条数"},
        ],
        output={"formats": ["csv"], "report": True},
    )
    summary = re.execute_rule(rule, base_output=str(tmp_path))
    result = pd.read_csv(os.path.join(str(tmp_path), summary["run_id"], "result.csv"))
    assert result.to_dict("records") == [
        {"community": "Alpha", "条数": 1},
        {"community": "Beta", "条数": 1},
    ]
    assert summary["task"]["task_type"] == "summary"


def test_workbench_renders_alias_field_and_column_hints(tmp_path):
    import webapp

    reply = webapp._workbench(
        {}, str(tmp_path), intent="按小区统计条数", source_path="lots.csv",
        source_format="csv", column_aliases="地区 = 市",
    )
    body = reply.body
    assert 'name="column_aliases"' in body
    assert "地区 = 市" in body
    assert "已探测" in body and "小区" in body


def test_web_plan_accepts_explicit_rule_json(tmp_path):
    """无 LLM 凭据时，工作台通过显式规则 JSON 完成 /plan。"""
    import webapp

    state = webapp.AppState(str(tmp_path))
    form = {
        "intent": [USER_INTENT],
        "source_path": ["lots.csv"],
        "source_format": ["csv"],
        "columns": [",".join(USER_COLUMNS)],
        "column_aliases": ["地区 = 市"],
        "rule_json": [json.dumps(make_rule(source_path="lots.csv", steps=[
            {"op": "filter", "column": "市", "operator": "eq", "value": "福州"},
            {"op": "count_rows", "by": "小区", "alias": "条数"},
        ]), ensure_ascii=False)],
        "formats": ["csv"],
    }
    reply = webapp.handle_request("POST", "/plan", {}, form, state)
    assert reply.status == 200
    assert "已应用列名映射" in reply.body
    assert "地区 → 市" in reply.body
    assert "校验未通过" not in reply.body


# ============================================================
# P1：新增算子冒烟
# ============================================================
def test_convert_unit_handles_chinese_money_and_area():
    df = pd.DataFrame({"金额": ["1.2万", "¥1,234", "100万元", None],
                       "面积": ["120㎡", "80平方米", "1.5亩", ""]})
    out, _ = re.apply_step(df, {"op": "convert_unit", "columns": ["金额"], "kind": "money", "keep_raw": True})
    assert out["金额"].iloc[0] == pytest.approx(12000)
    assert out["金额"].iloc[1] == pytest.approx(1234)
    assert out["金额"].iloc[2] == pytest.approx(1_000_000)
    assert "金额_raw" in out.columns
    area, _ = re.apply_step(df, {"op": "convert_unit", "columns": ["面积"], "kind": "area"})
    assert area["面积"].iloc[0] == pytest.approx(120)
    assert area["面积"].iloc[2] == pytest.approx(1000, abs=1)


def test_text_normalize_and_category_map():
    df = pd.DataFrame({
        "名称": ["ＡＢ\u200b公司", "ＡＢ 公司", "ＣＤ公司"],
        "城市": ["北京市", "上海", "天津"],
    })
    out, _ = re.apply_step(df, {"op": "text_normalize", "columns": ["名称"]})
    assert out["名称"].iloc[0] == "AB公司" and out["名称"].iloc[1] == "AB 公司"
    mapped, _ = re.apply_step(df, {"op": "category_map", "column": "城市",
                                   "mapping": {"北京市": "北京"}, "min_count": 2})
    assert mapped["城市"].iloc[0] == "北京"
    assert set(mapped["城市"].iloc[1:]) == {"其他"}


def test_dedupe_fuzzy_merges_name_variants():
    df = pd.DataFrame({"名称": ["XX置业有限公司", "XX置业公司", "YY集团"]})
    out, _ = re.apply_step(df, {"op": "dedupe_fuzzy", "columns": ["名称"]})
    assert len(out) == 2


def test_assert_profile_mask_pii():
    df = pd.DataFrame({"金额": [10, -1, 5], "手机": ["13812345678", "13900000000", "13011112222"]})
    flagged, _ = re.apply_step(df, {"op": "assert", "checks": [
        {"column": "金额", "operator": "gt", "value": 0}], "action": "flag", "into": "金额合规"})
    assert int(flagged["金额合规"].sum()) == 2
    profiled, _ = re.apply_step(df, {"op": "profile"})
    assert "缺失率(%)" in profiled.columns and len(profiled) == 2
    masked, _ = re.apply_step(df, {"op": "mask_pii", "columns": ["手机"], "kinds": ["phone"]})
    assert "13812345678" not in masked["手机"].iloc[0] and "*" in masked["手机"].iloc[0]


def test_time_ops_resample_rolling_lag_parse():
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=6, freq="D"),
        "v": [1, 2, 3, 4, 5, 6],
    })
    res, _ = re.apply_step(df, {"op": "resample", "column": "date", "freq": "M", "funcs": ["sum", "count"]})
    assert len(res) == 1 and "v_sum" in res.columns
    rolled, _ = re.apply_step(df, {"op": "rolling", "column": "v", "window": 2, "func": "mean"})
    assert rolled["v_2期mean"].iloc[1] == pytest.approx(1.5)
    lagged, _ = re.apply_step(df, {"op": "lag", "columns": ["v"], "periods": 1})
    assert pd.isna(lagged["v_lag1"].iloc[0]) and lagged["v_lag1"].iloc[1] == 1
    parsed, _ = re.apply_step(
        pd.DataFrame({"d": ["2024年1月1日", "2024/01/02"]}),
        {"op": "date_parse", "columns": ["d"], "formats": ["%Y年%m月%d日", "%Y/%m/%d"]},
    )
    assert parsed["d"].notna().all()
    assert detect_task_type({"steps": [{"op": "resample"}]}) == "trend"


def test_analysis_ops_corr_having_group_rank():
    df = pd.DataFrame({
        "g": ["A", "A", "B", "B"],
        "x": [1, 2, 3, 4], "y": [2, 4, 6, 8],
    })
    corr, _ = re.apply_step(df, {"op": "corr", "columns": ["x", "y"]})
    assert corr.loc[0, "y"] == pytest.approx(1.0)
    assert detect_task_type({"steps": [{"op": "corr"}]}) == "correlation"
    agg, _ = re.apply_step(df, {"op": "count_rows", "by": "g", "alias": "条数"})
    kept, _ = re.apply_step(agg, {"op": "having", "column": "条数", "topn": 1})
    assert len(kept) == 1
    ranked, _ = re.apply_step(df, {"op": "group_rank", "by": "g", "column": "x", "topn": 1})
    assert len(ranked) == 2


def test_set_joins_and_cross_column_filter():
    df = pd.DataFrame({"key": [1, 2, 3], "a": [1, 5, 9], "b": [2, 4, 10]})
    frames = {"right": pd.DataFrame({"key": [2]})}
    semi, _ = re.apply_step(df, {"op": "semi_join", "right": "right", "left_on": "key", "right_on": "key"}, frames)
    anti, _ = re.apply_step(df, {"op": "anti_join", "right": "right", "left_on": "key", "right_on": "key"}, frames)
    assert list(semi["key"]) == [2] and list(anti["key"]) == [1, 3]
    cross, _ = re.apply_step(df, {"op": "cross_column_filter", "left": "a", "right": "b", "operator": "gt"})
    assert list(cross["key"]) == [2]
